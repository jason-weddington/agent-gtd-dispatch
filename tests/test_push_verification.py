"""Tests for verify_pushes() and get_head_sha() in dispatch.py."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agent_gtd_dispatch import config
from agent_gtd_dispatch.dispatch import verify_pushes
from agent_gtd_dispatch.models import PushStatus, RepoPushStatus


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Set required env vars and use tmp path for workspace/db."""
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        "AGENT_SUBPROCESS_USER": "",
    }
    with patch.dict(os.environ, env):
        config.load()
        yield


def _make_completed(returncode=0, stdout="", stderr=""):
    """Build a fake subprocess.CompletedProcess."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _mock_run_side_effects(
    local_sha="abc1234",
    commits_ahead="3",
    remote_line="",
    dirty_output="",
):
    """Return a side_effect list for subprocess.run matching verify_pushes call order."""
    return [
        _make_completed(0, local_sha + "\n"),  # git rev-parse HEAD
        _make_completed(0, commits_ahead + "\n"),  # git rev-list base..HEAD --count
        _make_completed(0, remote_line),  # git ls-remote origin refs/heads/...
        _make_completed(0, dirty_output),  # git status --porcelain --untracked-files=no
    ]


class TestVerifyPushesClassification:
    def test_pushed_status(self, tmp_path) -> None:
        """remote_sha == local_sha and commits_ahead > 0 → pushed."""
        sha = "abc1234def5678"
        remote_line = f"{sha}\trefs/heads/feat/x\n"
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = _mock_run_side_effects(
                local_sha=sha, commits_ahead="2", remote_line=remote_line
            )
            results = verify_pushes([("myrepo", tmp_path, "base000")], "feat/x")

        assert len(results) == 1
        r = results[0]
        assert r.status == PushStatus.pushed
        assert r.local_sha == sha
        assert r.remote_sha == sha
        assert r.commits_ahead == 2
        assert r.dirty is False

    def test_no_changes_status(self, tmp_path) -> None:
        """commits_ahead == 0 → no_changes regardless of remote."""
        sha = "abc1234def5678"
        remote_line = f"{sha}\trefs/heads/feat/x\n"
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = _mock_run_side_effects(
                local_sha=sha, commits_ahead="0", remote_line=remote_line
            )
            results = verify_pushes([("myrepo", tmp_path, "base000")], "feat/x")

        assert results[0].status == PushStatus.no_changes
        assert results[0].commits_ahead == 0

    def test_unpushed_status_remote_missing(self, tmp_path) -> None:
        """commits_ahead > 0, remote branch missing → unpushed."""
        sha = "abc1234def5678"
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = _mock_run_side_effects(
                local_sha=sha, commits_ahead="1", remote_line=""
            )
            results = verify_pushes([("myrepo", tmp_path, "base000")], "feat/x")

        assert results[0].status == PushStatus.unpushed
        assert results[0].remote_sha is None

    def test_unpushed_status_partial_push(self, tmp_path) -> None:
        """commits_ahead > 0, remote_sha != local_sha → unpushed."""
        local_sha = "aaaa1234"
        remote_sha = "bbbb5678"
        remote_line = f"{remote_sha}\trefs/heads/feat/x\n"
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = _mock_run_side_effects(
                local_sha=local_sha, commits_ahead="2", remote_line=remote_line
            )
            results = verify_pushes([("myrepo", tmp_path, "base000")], "feat/x")

        assert results[0].status == PushStatus.unpushed
        assert results[0].local_sha == local_sha
        assert results[0].remote_sha == remote_sha


class TestVerifyPushesEdgeCases:
    def test_precedence_no_changes_over_remote(self, tmp_path) -> None:
        """commits_ahead == 0 → no_changes even if remote SHA matches local."""
        sha = "abc1234def5678"
        remote_line = f"{sha}\trefs/heads/feat/x\n"
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = _mock_run_side_effects(
                local_sha=sha, commits_ahead="0", remote_line=remote_line
            )
            results = verify_pushes([("myrepo", tmp_path, "base000")], "feat/x")

        assert results[0].status == PushStatus.no_changes

    def test_git_command_failure_fail_closed(self, tmp_path) -> None:
        """Any git subprocess failure → fail-closed unpushed with pinned values."""
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.return_value = _make_completed(returncode=1, stderr="fatal error")
            results = verify_pushes([("myrepo", tmp_path, "base000")], "feat/x")

        assert len(results) == 1
        r = results[0]
        assert r.status == PushStatus.unpushed
        assert r.local_sha is None
        assert r.remote_sha is None
        assert r.commits_ahead == 0
        assert r.dirty is False

    def test_git_exception_fail_closed(self, tmp_path) -> None:
        """Subprocess raising an exception → fail-closed unpushed."""
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("subprocess not found")
            results = verify_pushes([("myrepo", tmp_path, "base000")], "feat/x")

        r = results[0]
        assert r.status == PushStatus.unpushed
        assert r.local_sha is None


class TestVerifyPushesDirty:
    def test_dirty_false_when_only_untracked_files(self, tmp_path) -> None:
        """git status --porcelain --untracked-files=no returns empty → dirty=False."""
        sha = "abc1234"
        remote_line = f"{sha}\trefs/heads/feat/x\n"
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = _mock_run_side_effects(
                local_sha=sha,
                commits_ahead="1",
                remote_line=remote_line,
                dirty_output="",
            )
            results = verify_pushes([("myrepo", tmp_path, "base000")], "feat/x")

        assert results[0].dirty is False

    def test_dirty_true_on_modified_tracked_file(self, tmp_path) -> None:
        """Non-empty porcelain output → dirty=True."""
        sha = "abc1234"
        remote_line = f"{sha}\trefs/heads/feat/x\n"
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = _mock_run_side_effects(
                local_sha=sha,
                commits_ahead="1",
                remote_line=remote_line,
                dirty_output=" M src/foo.py\n",
            )
            results = verify_pushes([("myrepo", tmp_path, "base000")], "feat/x")

        assert results[0].dirty is True

    def test_dirty_never_fails_the_classification(self, tmp_path) -> None:
        """dirty=True on a pushed repo → still pushed (dirty is not a failure)."""
        sha = "abc1234"
        remote_line = f"{sha}\trefs/heads/feat/x\n"
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = _mock_run_side_effects(
                local_sha=sha,
                commits_ahead="1",
                remote_line=remote_line,
                dirty_output=" M README.md\n",
            )
            results = verify_pushes([("myrepo", tmp_path, "base000")], "feat/x")

        assert results[0].status == PushStatus.pushed
        assert results[0].dirty is True


class TestVerifyPushesMultiRepo:
    def test_multi_repo_returns_results_in_order(self, tmp_path) -> None:
        """Multi-repo input returns per-repo statuses in input order."""
        sha_a = "aaaa1234"
        sha_b = "bbbb5678"
        repo_a = tmp_path / "repo_a"
        repo_b = tmp_path / "repo_b"
        repo_a.mkdir()
        repo_b.mkdir()
        remote_a = f"{sha_a}\trefs/heads/feat/x\n"
        remote_b = f"{sha_b}\trefs/heads/feat/x\n"

        side_effects = (
            # repo_a: pushed (commits_ahead=1, remote==local)
            _mock_run_side_effects(
                local_sha=sha_a, commits_ahead="1", remote_line=remote_a
            )
            # repo_b: no_changes
            + _mock_run_side_effects(
                local_sha=sha_b, commits_ahead="0", remote_line=remote_b
            )
        )
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = side_effects
            results = verify_pushes(
                [("repo_a", repo_a, "base000"), ("repo_b", repo_b, "base111")],
                "feat/x",
            )

        assert len(results) == 2
        assert results[0].repo_name == "repo_a"
        assert results[0].status == PushStatus.pushed
        assert results[1].repo_name == "repo_b"
        assert results[1].status == PushStatus.no_changes

    def test_two_failing_repos_error_string(self, tmp_path) -> None:
        """Two unpushed repos → exact joined error string format."""
        repo_a = tmp_path / "repo_a"
        repo_b = tmp_path / "repo_b"
        repo_a.mkdir()
        repo_b.mkdir()

        # Both repos have commits_ahead > 0 but remote doesn't match
        sha_a = "aaaa1234"
        sha_b = "bbbb5678"
        # remote returns different SHA → unpushed
        remote_a = "cccc9999\trefs/heads/feat/x\n"
        remote_b = "dddd8888\trefs/heads/feat/x\n"

        side_effects = _mock_run_side_effects(
            local_sha=sha_a, commits_ahead="2", remote_line=remote_a
        ) + _mock_run_side_effects(
            local_sha=sha_b, commits_ahead="3", remote_line=remote_b
        )
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = side_effects
            results = verify_pushes(
                [("repo_a", repo_a, "base000"), ("repo_b", repo_b, "base111")],
                "feat/x",
            )

        unpushed = [r for r in results if r.status == PushStatus.unpushed]
        assert len(unpushed) == 2

        # Build the expected error string (same logic as _dispatch_worker)
        fragments = []
        for r in unpushed:
            if r.local_sha is not None:
                fragments.append(
                    f"{r.repo_name}: {r.commits_ahead} unpushed commit(s) on {r.branch}"
                )
            else:
                fragments.append(f"{r.repo_name}: verification error on {r.branch}")
        error_str = "push verification failed: " + "; ".join(fragments)

        assert error_str == (
            "push verification failed: "
            "repo_a: 2 unpushed commit(s) on feat/x; "
            "repo_b: 3 unpushed commit(s) on feat/x"
        )


class TestVerifyPushesRepoName:
    def test_monorepo_comment_uses_origin_derived_name(self, tmp_path) -> None:
        """Monorepo path uses repo_name_from_origin — NOT the workspace dir name."""
        from agent_gtd_dispatch.dispatch import repo_name_from_origin

        git_origin = "git@host:repos/agent-gtd-dispatch"
        expected_repo_name = repo_name_from_origin(git_origin)
        # It should be 'repos-agent-gtd-dispatch', NOT something like 'repos-agent-gtd-dispatch-abc123'
        assert "-" in expected_repo_name
        # Make sure it doesn't contain a run-id suffix
        run_id = "abc123xyz"
        assert run_id not in expected_repo_name

        # Simulate the monorepo verify_pushes call
        sha = "deadbeef1234"
        remote_line = f"{sha}\trefs/heads/feat/x\n"
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = _mock_run_side_effects(
                local_sha=sha, commits_ahead="1", remote_line=remote_line
            )
            results = verify_pushes(
                [(expected_repo_name, tmp_path, "base000")], "feat/x"
            )

        assert results[0].repo_name == expected_repo_name
        assert run_id not in results[0].repo_name


class TestVerifyPushesCmds:
    def test_passes_branch_name_to_ls_remote(self, tmp_path) -> None:
        """git ls-remote uses refs/heads/<branch_name>."""
        sha = "abc1234"
        branch = "feat/test-branch"
        remote_line = f"{sha}\trefs/heads/{branch}\n"
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = _mock_run_side_effects(
                local_sha=sha, commits_ahead="1", remote_line=remote_line
            )
            verify_pushes([("myrepo", tmp_path, "base000")], branch)

        # Third call should be ls-remote with correct refspec
        ls_remote_call = mock_run.call_args_list[2]
        cmd_arg = ls_remote_call[0][0]  # positional first arg is cmd list
        assert "ls-remote" in cmd_arg
        assert f"refs/heads/{branch}" in cmd_arg

    def test_status_uses_untracked_files_no(self, tmp_path) -> None:
        """git status uses --untracked-files=no to exclude transcript/attachments."""
        sha = "abc1234"
        remote_line = f"{sha}\trefs/heads/feat/x\n"
        with patch("agent_gtd_dispatch.dispatch.subprocess.run") as mock_run:
            mock_run.side_effect = _mock_run_side_effects(
                local_sha=sha, commits_ahead="1", remote_line=remote_line
            )
            verify_pushes([("myrepo", tmp_path, "base000")], "feat/x")

        # Fourth call is git status
        status_call = mock_run.call_args_list[3]
        cmd_arg = status_call[0][0]
        assert "--untracked-files=no" in cmd_arg


class TestWorkerVerification:
    """Worker-level integration tests for push verification wiring in _dispatch_worker."""

    @pytest.fixture
    def _base_mocks(self, tmp_path):
        """Common mock setup for _dispatch_worker tests."""
        from agent_gtd_dispatch.models import DispatchMode, Run

        run = Run(
            item_id="item123",
            project_name="TestProject",
            branch_name="feat/abc-fix",
            mode=DispatchMode.BUILD,
        )
        return run

    @pytest.mark.asyncio
    async def test_build_unpushed_fails_run_and_preserves_workspace(
        self, tmp_path
    ) -> None:
        """Unpushed → run failed + pinned error + comment posted + cleanup NOT called."""
        from unittest.mock import AsyncMock, patch

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.models import DispatchMode, PushStatus, Run

        await db.init_db()
        run = Run(
            item_id="item123",
            project_name="TestProject",
            branch_name="feat/abc-fix",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        # One unpushed repo result
        unpushed_result = RepoPushStatus(
            repo_name="repos-testproj",
            branch="feat/abc-fix",
            status=PushStatus.unpushed,
            local_sha="deadbeef",
            remote_sha=None,
            commits_ahead=2,
            dirty=False,
        )

        completed_result = MagicMock()
        completed_result.returncode = 0

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_gtd.get_item = AsyncMock(
                return_value={
                    "id": "item123",
                    "title": "Fix bug",
                    "project_id": "proj1",
                }
            )
            mock_gtd.get_project = AsyncMock(
                return_value={
                    "id": "proj1",
                    "name": "TestProject",
                    "git_origin": "git@host:repos/testproj",
                }
            )
            mock_gtd.post_comment = AsyncMock()
            mock_gtd.list_attachments = AsyncMock(return_value=[])

            fake_workspace = tmp_path / "repos-testproj-abc"
            fake_workspace.mkdir()
            mock_dispatch.prepare_workspace = MagicMock(return_value=fake_workspace)
            mock_dispatch.get_head_sha = MagicMock(return_value="baseshaabc")
            mock_dispatch.repo_name_from_origin = MagicMock(
                return_value="repos-testproj"
            )
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt = MagicMock(return_value="prompt text")
            mock_dispatch.run_agent = AsyncMock(return_value=completed_result)
            mock_dispatch.verify_pushes = MagicMock(return_value=[unpushed_result])
            mock_dispatch.cleanup_workspace = MagicMock()

            from agent_gtd_dispatch.engines import CLAUDE
            from agent_gtd_dispatch.main import _dispatch_worker

            await _dispatch_worker(run, 50, CLAUDE, 600)

        # Run should be failed
        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "failed"
        assert updated.error is not None
        assert "push verification failed" in updated.error
        assert "repos-testproj: 2 unpushed commit(s) on feat/abc-fix" in updated.error

        # cleanup_workspace must NOT have been called
        mock_dispatch.cleanup_workspace.assert_not_called()

        # Comment should have been posted
        mock_gtd.post_comment.assert_called()
        # Find the verification comment (not dispatch comment)
        comment_calls = [str(c) for c in mock_gtd.post_comment.call_args_list]
        verification_comment = next(
            (c for c in comment_calls if "Push verification failed" in c), None
        )
        assert verification_comment is not None

    @pytest.mark.asyncio
    async def test_build_all_pushed_marks_succeeded_no_comment(self, tmp_path) -> None:
        """All pushed/no_changes → succeeded with no verification comment."""
        from unittest.mock import AsyncMock, patch

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.models import DispatchMode, PushStatus, Run

        await db.init_db()
        run = Run(
            item_id="item456",
            project_name="TestProject",
            branch_name="feat/def-feature",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        pushed_result = RepoPushStatus(
            repo_name="repos-testproj",
            branch="feat/def-feature",
            status=PushStatus.pushed,
            local_sha="aabbccdd",
            remote_sha="aabbccdd",
            commits_ahead=3,
            dirty=False,
        )

        completed_result = MagicMock()
        completed_result.returncode = 0

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_gtd.get_item = AsyncMock(
                return_value={
                    "id": "item456",
                    "title": "Add feature",
                    "project_id": "proj1",
                }
            )
            mock_gtd.get_project = AsyncMock(
                return_value={
                    "id": "proj1",
                    "name": "TestProject",
                    "git_origin": "git@host:repos/testproj",
                }
            )
            mock_gtd.post_comment = AsyncMock()
            mock_gtd.list_attachments = AsyncMock(return_value=[])

            fake_workspace = tmp_path / "repos-testproj-def"
            fake_workspace.mkdir()
            mock_dispatch.prepare_workspace = MagicMock(return_value=fake_workspace)
            mock_dispatch.get_head_sha = MagicMock(return_value="baseshaxyz")
            mock_dispatch.repo_name_from_origin = MagicMock(
                return_value="repos-testproj"
            )
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt = MagicMock(return_value="prompt text")
            mock_dispatch.run_agent = AsyncMock(return_value=completed_result)
            mock_dispatch.verify_pushes = MagicMock(return_value=[pushed_result])
            mock_dispatch.cleanup_workspace = MagicMock()

            from agent_gtd_dispatch.engines import CLAUDE
            from agent_gtd_dispatch.main import _dispatch_worker

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "succeeded"

        # No verification comment should be posted (only the dispatch comment)
        verification_comments = [
            c
            for c in mock_gtd.post_comment.call_args_list
            if "Push verification failed" in str(c)
        ]
        assert len(verification_comments) == 0

    @pytest.mark.asyncio
    async def test_plan_mode_zero_verification_git_calls(self, tmp_path) -> None:
        """Plan-mode run with returncode 0 performs zero verification git calls."""
        from unittest.mock import AsyncMock, patch

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.models import DispatchMode, Run

        await db.init_db()
        run = Run(
            item_id="item789",
            project_name="TestProject",
            branch_name=None,
            mode=DispatchMode.PLAN,
        )
        await db.insert_run(run)

        completed_result = MagicMock()
        completed_result.returncode = 0

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_gtd.get_item = AsyncMock(
                return_value={
                    "id": "item789",
                    "title": "Plan feature",
                    "project_id": "proj1",
                }
            )
            mock_gtd.get_project = AsyncMock(
                return_value={
                    "id": "proj1",
                    "name": "TestProject",
                    "git_origin": "git@host:repos/testproj",
                }
            )
            mock_gtd.post_comment = AsyncMock()
            mock_gtd.list_attachments = AsyncMock(return_value=[])

            fake_workspace = tmp_path / "repos-testproj-plan"
            fake_workspace.mkdir()
            mock_dispatch.prepare_workspace = MagicMock(return_value=fake_workspace)
            mock_dispatch.get_head_sha = MagicMock(return_value="baseshaxyz")
            mock_dispatch.repo_name_from_origin = MagicMock(
                return_value="repos-testproj"
            )
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt = MagicMock(return_value="prompt text")
            mock_dispatch.run_agent = AsyncMock(return_value=completed_result)
            mock_dispatch.verify_pushes = MagicMock(return_value=[])
            mock_dispatch.cleanup_workspace = MagicMock()

            from agent_gtd_dispatch.engines import CLAUDE
            from agent_gtd_dispatch.main import _dispatch_worker

            await _dispatch_worker(run, 50, CLAUDE, 600)

        # verify_pushes must NOT have been called for plan mode
        mock_dispatch.verify_pushes.assert_not_called()
        # get_head_sha must NOT have been called for plan mode
        mock_dispatch.get_head_sha.assert_not_called()

    # -----------------------------------------------------------------------
    # Timeout-path reclassification (linger-success) — new tests
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_timeout_all_pushed_reclassified_as_succeeded(self, tmp_path) -> None:
        """BUILD + TimeoutExpired + all repos pushed → run reclassified as succeeded.

        Verifies: db status == 'succeeded', verify_pushes called, comment does
        NOT contain 'may need to be broken down', _publish_run_event fired with
        'succeeded' and never 'timed_out' for this run.
        """
        from unittest.mock import AsyncMock, patch

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.models import DispatchMode, PushStatus, Run

        await db.init_db()
        run = Run(
            item_id="item-timeout-success",
            project_name="TestProject",
            branch_name="feat/timeout-success",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        pushed_result = RepoPushStatus(
            repo_name="repos-testproj",
            branch="feat/timeout-success",
            status=PushStatus.pushed,
            local_sha="aabbccdd",
            remote_sha="aabbccdd",
            commits_ahead=3,
            dirty=False,
        )

        events: list[tuple] = []

        def _capture_event(run_id: str, status: str, ts: object) -> None:
            events.append((run_id, status, ts))

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            patch(
                "agent_gtd_dispatch.main._publish_run_event",
                side_effect=_capture_event,
            ),
        ):
            mock_gtd.get_item = AsyncMock(
                return_value={
                    "id": "item-timeout-success",
                    "title": "Fix bug",
                    "project_id": "proj1",
                }
            )
            mock_gtd.get_project = AsyncMock(
                return_value={
                    "id": "proj1",
                    "name": "TestProject",
                    "git_origin": "git@host:repos/testproj",
                }
            )
            mock_gtd.post_comment = AsyncMock()
            mock_gtd.list_attachments = AsyncMock(return_value=[])

            fake_workspace = tmp_path / "repos-testproj-timeout"
            fake_workspace.mkdir()
            mock_dispatch.prepare_workspace = MagicMock(return_value=fake_workspace)
            mock_dispatch.get_head_sha = MagicMock(return_value="baseshaabc")
            mock_dispatch.repo_name_from_origin = MagicMock(
                return_value="repos-testproj"
            )
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt = MagicMock(return_value="prompt text")
            mock_dispatch.run_agent = AsyncMock(
                side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=600)
            )
            mock_dispatch.verify_pushes = MagicMock(return_value=[pushed_result])
            mock_dispatch.cleanup_workspace = MagicMock()

            from agent_gtd_dispatch.engines import CLAUDE
            from agent_gtd_dispatch.main import _dispatch_worker

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "succeeded"

        mock_dispatch.verify_pushes.assert_called_once()

        # Comment must signal linger-success (not the genuine-timeout phrasing)
        assert mock_gtd.post_comment.call_count >= 1
        all_comment_texts = [
            str(c.args[1]) if c.args else str(c.kwargs.get("content", ""))
            for c in mock_gtd.post_comment.call_args_list
        ]
        final_comment = all_comment_texts[-1]
        assert "may need to be broken down" not in final_comment
        assert "marking run succeeded" in final_comment

        # _publish_run_event: 'succeeded' must appear, 'timed_out' must not
        assert (run.id, "succeeded") in [(rid, st) for rid, st, _ in events]
        assert (run.id, "timed_out") not in [(rid, st) for rid, st, _ in events]

    @pytest.mark.asyncio
    async def test_timeout_unpushed_stays_timed_out(self, tmp_path) -> None:
        """BUILD + TimeoutExpired + unpushed repo → run finalized as timed_out.

        Verifies: db status == 'timed_out', error text, and timeout comment.
        """
        from unittest.mock import AsyncMock, patch

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.models import DispatchMode, PushStatus, Run

        await db.init_db()
        run = Run(
            item_id="item-timeout-unpushed",
            project_name="TestProject",
            branch_name="feat/timeout-unpushed",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        unpushed_result = RepoPushStatus(
            repo_name="repos-testproj",
            branch="feat/timeout-unpushed",
            status=PushStatus.unpushed,
            local_sha="deadbeef",
            remote_sha=None,
            commits_ahead=2,
            dirty=False,
        )

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_gtd.get_item = AsyncMock(
                return_value={
                    "id": "item-timeout-unpushed",
                    "title": "Fix bug",
                    "project_id": "proj1",
                }
            )
            mock_gtd.get_project = AsyncMock(
                return_value={
                    "id": "proj1",
                    "name": "TestProject",
                    "git_origin": "git@host:repos/testproj",
                }
            )
            mock_gtd.post_comment = AsyncMock()
            mock_gtd.list_attachments = AsyncMock(return_value=[])

            fake_workspace = tmp_path / "repos-testproj-timeout-unpushed"
            fake_workspace.mkdir()
            mock_dispatch.prepare_workspace = MagicMock(return_value=fake_workspace)
            mock_dispatch.get_head_sha = MagicMock(return_value="baseshaabc")
            mock_dispatch.repo_name_from_origin = MagicMock(
                return_value="repos-testproj"
            )
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt = MagicMock(return_value="prompt text")
            mock_dispatch.run_agent = AsyncMock(
                side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=600)
            )
            mock_dispatch.verify_pushes = MagicMock(return_value=[unpushed_result])
            mock_dispatch.cleanup_workspace = MagicMock()

            from agent_gtd_dispatch.engines import CLAUDE
            from agent_gtd_dispatch.main import _dispatch_worker

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "timed_out"
        assert updated.error == f"Timed out after {600}s"

        # Genuine-timeout comment must be posted
        assert mock_gtd.post_comment.call_count >= 1
        all_comment_texts = [
            str(c.args[1]) if c.args else str(c.kwargs.get("content", ""))
            for c in mock_gtd.post_comment.call_args_list
        ]
        timeout_comments = [
            t for t in all_comment_texts if "timed out after" in t.lower()
        ]
        assert timeout_comments, "expected a timed-out comment"

    @pytest.mark.asyncio
    async def test_plan_mode_timeout_skips_verify_pushes(self, tmp_path) -> None:
        """PLAN mode + TimeoutExpired → timed_out, verify_pushes never called.

        Plan runs don't push code, so push-verification must be skipped entirely.
        """
        from unittest.mock import AsyncMock, patch

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.models import DispatchMode, Run

        await db.init_db()
        run = Run(
            item_id="item-plan-timeout",
            project_name="TestProject",
            branch_name=None,
            mode=DispatchMode.PLAN,
        )
        await db.insert_run(run)

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_gtd.get_item = AsyncMock(
                return_value={
                    "id": "item-plan-timeout",
                    "title": "Plan feature",
                    "project_id": "proj1",
                }
            )
            mock_gtd.get_project = AsyncMock(
                return_value={
                    "id": "proj1",
                    "name": "TestProject",
                    "git_origin": "git@host:repos/testproj",
                }
            )
            mock_gtd.post_comment = AsyncMock()
            mock_gtd.list_attachments = AsyncMock(return_value=[])

            fake_workspace = tmp_path / "repos-testproj-plan-timeout"
            fake_workspace.mkdir()
            mock_dispatch.prepare_workspace = MagicMock(return_value=fake_workspace)
            mock_dispatch.get_head_sha = MagicMock(return_value="baseshaxyz")
            mock_dispatch.repo_name_from_origin = MagicMock(
                return_value="repos-testproj"
            )
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt = MagicMock(return_value="prompt text")
            mock_dispatch.run_agent = AsyncMock(
                side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=600)
            )
            mock_dispatch.verify_pushes = MagicMock(
                return_value=[
                    RepoPushStatus(
                        repo_name="repos-testproj",
                        branch="feat/plan-test",
                        status=PushStatus.pushed,
                        local_sha="abc",
                        remote_sha="abc",
                        commits_ahead=1,
                        dirty=False,
                    )
                ]
            )
            mock_dispatch.cleanup_workspace = MagicMock()

            from agent_gtd_dispatch.engines import CLAUDE
            from agent_gtd_dispatch.main import _dispatch_worker

            await _dispatch_worker(run, 50, CLAUDE, 600)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status.value == "timed_out"

        # verify_pushes must NOT be called for plan mode
        mock_dispatch.verify_pushes.assert_not_called()
