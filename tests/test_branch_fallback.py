"""The ``<branch>-r<N>`` push fallback for a rebuild whose item branch is taken.

A re-dispatched item reuses its deterministic branch name. When the earlier
run's branch was never deleted, the rebuild (cloned from a fresh main) cannot
fast-forward it, and both the worker push and the rescue push used to be
rejected — leaving gate-green work only in the clone. These tests drive real
git: a bare origin, an "old run" clone that leaves commits on ``feat/x``, and a
fresh clone that builds on main.
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_gtd_dispatch import config, dispatch
from agent_gtd_dispatch.models import DispatchMode, PushStatus, RepoPushStatus, Run
from tests.completion_fixtures import seed_build_evidence
from tests.worker_mocks import stub_rescue

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _remote_sha(origin: Path, branch: str) -> str:
    out = subprocess.run(
        ["git", "ls-remote", str(origin), f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return out.split("\t")[0] if out else ""


def _commit(repo: Path, name: str, text: str = "x\n") -> str:
    (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"feat: {name}")
    return _git(repo, "rev-parse", "HEAD")


class _Origin:
    """A bare origin with ``main``, plus helpers to clone it."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.path = tmp_path / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.path)],
            check=True,
            capture_output=True,
        )
        seed = tmp_path / "seed"
        seed.mkdir()
        _git(seed, "init", "-b", "main")
        _commit(seed, "base.txt", "base\n")
        _git(seed, "remote", "add", "origin", str(self.path))
        _git(seed, "push", "-u", "origin", "main")

    def clone(self, name: str, branch: str = "feat/x") -> Path:
        repo = self.tmp / name
        subprocess.run(
            ["git", "clone", str(self.path), str(repo)],
            check=True,
            capture_output=True,
        )
        _git(repo, "checkout", "-b", branch)
        return repo

    def leave_old_run_on(self, branch: str) -> str:
        """Push an unrelated commit to *branch*, as an earlier run did."""
        old = self.clone(f"old-{branch.replace('/', '-')}", branch)
        sha = _commit(old, f"old_{branch.replace('/', '_')}.py", "old\n")
        _git(old, "push", "-u", "origin", branch)
        return sha


class TestPushFallback:
    def test_a_taken_branch_falls_back_to_r2_and_is_left_untouched(
        self, tmp_path
    ) -> None:
        """AC6(a)."""
        origin = _Origin(tmp_path)
        old_sha = origin.leave_old_run_on("feat/x")
        repo = origin.clone("rebuild")
        head = _commit(repo, "new.py")

        attempt = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)

        assert attempt.proc.returncode == 0
        assert attempt.fell_back is True
        assert attempt.branch == "feat/x-r2"
        assert _remote_sha(origin.path, "feat/x-r2") == head
        assert _remote_sha(origin.path, "feat/x") == old_sha

    def test_r2_also_taken_falls_back_to_r3(self, tmp_path) -> None:
        """AC6(b)."""
        origin = _Origin(tmp_path)
        old_sha = origin.leave_old_run_on("feat/x")
        r2_sha = origin.leave_old_run_on("feat/x-r2")
        repo = origin.clone("rebuild")
        head = _commit(repo, "new.py")

        attempt = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)

        assert attempt.branch == "feat/x-r3"
        assert attempt.fell_back is True
        assert _remote_sha(origin.path, "feat/x-r3") == head
        assert _remote_sha(origin.path, "feat/x") == old_sha
        assert _remote_sha(origin.path, "feat/x-r2") == r2_sha

    def test_fast_forward_still_pushes_to_the_branch(self, tmp_path) -> None:
        """AC6(c): origin's branch is an ancestor of HEAD — a plain push."""
        origin = _Origin(tmp_path)
        origin.leave_old_run_on("feat/x")
        repo = origin.clone("continue")
        _git(repo, "fetch", "origin", "feat/x")
        _git(repo, "reset", "--hard", "origin/feat/x")
        head = _commit(repo, "more.py")

        attempt = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)

        assert attempt.proc.returncode == 0
        assert attempt.fell_back is False
        assert attempt.branch == "feat/x"
        assert _remote_sha(origin.path, "feat/x") == head
        assert _remote_sha(origin.path, "feat/x-r2") == ""

    def test_absent_branch_is_created_without_fallback(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        repo = origin.clone("fresh")
        head = _commit(repo, "new.py")

        attempt = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)

        assert attempt.branch == "feat/x"
        assert attempt.fell_back is False
        assert _remote_sha(origin.path, "feat/x") == head

    def test_a_hook_rejection_is_not_treated_as_non_fast_forward(
        self, tmp_path
    ) -> None:
        """AC4: any other rejection keeps today's behaviour — no -rN branch."""
        origin = _Origin(tmp_path)
        repo = origin.clone("hooked")
        _commit(repo, "new.py")
        hook = repo / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\necho 'tests failed'\nexit 1\n")
        hook.chmod(0o755)

        attempt = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)

        assert attempt.proc.returncode != 0
        assert attempt.fell_back is False
        assert attempt.branch == "feat/x"
        assert _remote_sha(origin.path, "feat/x") == ""
        assert _remote_sha(origin.path, "feat/x-r2") == ""

    def test_a_hook_rejection_on_an_ancestor_branch_does_not_fall_back(
        self, tmp_path
    ) -> None:
        """The remote branch exists but IS an ancestor: not the fallback case."""
        origin = _Origin(tmp_path)
        old_sha = origin.leave_old_run_on("feat/x")
        repo = origin.clone("hooked")
        _git(repo, "fetch", "origin", "feat/x")
        _git(repo, "reset", "--hard", "origin/feat/x")
        _commit(repo, "more.py")
        hook = repo / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)

        attempt = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)

        assert attempt.proc.returncode != 0
        assert attempt.fell_back is False
        assert _remote_sha(origin.path, "feat/x") == old_sha
        assert _remote_sha(origin.path, "feat/x-r2") == ""

    def test_an_unreachable_origin_is_not_treated_as_non_fast_forward(
        self, tmp_path
    ) -> None:
        origin = _Origin(tmp_path)
        repo = origin.clone("offline")
        _commit(repo, "new.py")
        _git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

        attempt = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)

        assert attempt.proc.returncode != 0
        assert attempt.fell_back is False
        assert dispatch.is_non_fast_forward(repo, "feat/x") is False

    def test_never_force_pushes(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        origin.leave_old_run_on("feat/x")
        repo = origin.clone("rebuild")
        _commit(repo, "new.py")
        calls: list[list[str]] = []
        real_run = subprocess.run

        def _spy(cmd, *a, **kw):
            calls.append(list(cmd))
            return real_run(cmd, *a, **kw)

        with patch("agent_gtd_dispatch.dispatch.subprocess.run", side_effect=_spy):
            dispatch.push_unpushed_repo(repo, "feat/x", 60.0)

        pushes = [c for c in calls if "push" in c]
        assert len(pushes) == 2
        for cmd in pushes:
            assert not any(a.startswith("--force") or a == "-f" for a in cmd)
            assert not any(a.startswith("+") for a in cmd)
            assert "--delete" not in cmd


class TestVerifyPushesHonoursFallback:
    def test_a_repo_that_fell_back_is_verified_against_its_rn_branch(
        self, tmp_path
    ) -> None:
        origin = _Origin(tmp_path)
        origin.leave_old_run_on("feat/x")
        repo = origin.clone("rebuild")
        base = _git(repo, "rev-parse", "HEAD")
        _commit(repo, "new.py")
        dispatch.push_unpushed_repo(repo, "feat/x", 60.0)

        stale = dispatch.verify_pushes([("r", repo, base)], "feat/x")
        honoured = dispatch.verify_pushes(
            [("r", repo, base)], "feat/x", {"r": "feat/x-r2"}
        )

        assert stale[0].status == PushStatus.unpushed
        assert honoured[0].status == PushStatus.pushed
        assert honoured[0].branch == "feat/x-r2"


class TestRescueFallback:
    def test_rescue_onto_a_taken_branch_falls_back_to_r2(self, tmp_path) -> None:
        """AC6(d)."""
        origin = _Origin(tmp_path)
        old_sha = origin.leave_old_run_on("feat/x")
        repo = origin.clone("abandoned")
        (repo / "half_done.py").write_text("x = 1\n")

        result = dispatch.rescue_abandoned_work("repo-a", repo, "feat/x", "run-1")

        assert result.pushed is True
        assert result.committed is True
        assert result.branch == "feat/x-r2"
        assert _remote_sha(origin.path, "feat/x") == old_sha
        assert _remote_sha(origin.path, "feat/x-r2") == _git(repo, "rev-parse", "HEAD")

    def test_rescue_of_a_repo_already_on_its_fallback_branch(self, tmp_path) -> None:
        """A repo the backstop moved to -r2 is rescued onto -r2, not feat/x."""
        origin = _Origin(tmp_path)
        old_sha = origin.leave_old_run_on("feat/x")
        repo = origin.clone("rebuild")
        _commit(repo, "new.py")
        dispatch.push_unpushed_repo(repo, "feat/x", 60.0)
        (repo / "after.py").write_text("y = 2\n")

        result = dispatch.rescue_abandoned_work("repo-a", repo, "feat/x-r2", "run-1")

        assert result.pushed is True
        assert result.branch == "feat/x-r2"
        assert _remote_sha(origin.path, "feat/x-r2") == _git(repo, "rev-parse", "HEAD")
        assert _remote_sha(origin.path, "feat/x") == old_sha


class TestRescueReporting:
    @staticmethod
    def _run(workspace_path: str | None = None) -> Run:
        return Run(
            id="run-ret",
            item_id="item-ret",
            project_name="P",
            branch_name="feat/x",
            mode=DispatchMode.BUILD,
            workspace_path=workspace_path,
        )

    @pytest.mark.asyncio
    async def test_retained_path_comes_from_the_db_when_memory_is_stale(
        self, tmp_path
    ) -> None:
        """AC6(e): the in-memory Run never learned its workspace_path."""
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.main import _rescue_before_teardown

        await db.init_db()
        run = self._run()
        await db.insert_run(run)
        ws = tmp_path / "ws-run-ret"
        await db.update_run(run.id, workspace_path=str(ws))
        failed = dispatch.RescueResult(
            "repo-a", attempted=True, pushed=False, committed=True, error="refused"
        )
        with (
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
        ):
            mock_dispatch._executor = None
            mock_dispatch.rescue_abandoned_work = MagicMock(return_value=failed)
            mock_gtd.post_comment = AsyncMock()
            await _rescue_before_teardown(
                run, [("repo-a", ws / "repo-a", None)], attribution=None
            )

        stored = await db.get_run(run.id)
        assert stored is not None and stored.error is not None
        assert f"RETAINED at {ws}" in stored.error
        assert "None" not in stored.error

    @pytest.mark.asyncio
    async def test_monorepo_with_no_workspace_path_reports_the_clone_dir(
        self, tmp_path
    ) -> None:
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.main import _rescue_before_teardown

        await db.init_db()
        run = self._run()
        await db.insert_run(run)
        clone = tmp_path / "repo-clone"
        failed = dispatch.RescueResult(
            "repo", attempted=True, pushed=False, committed=False, error="refused"
        )
        with (
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
        ):
            mock_dispatch._executor = None
            mock_dispatch.rescue_abandoned_work = MagicMock(return_value=failed)
            mock_gtd.post_comment = AsyncMock()
            await _rescue_before_teardown(
                run, [("repo", clone, None)], attribution=None
            )

        stored = await db.get_run(run.id)
        assert stored is not None and stored.error is not None
        assert f"RETAINED at {clone}" in stored.error
        assert "None" not in stored.error
        comment = mock_gtd.post_comment.await_args.args[1]
        assert str(clone) in comment

    @pytest.mark.asyncio
    async def test_a_rescue_that_fell_back_records_and_names_the_rn_branch(
        self, tmp_path
    ) -> None:
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.main import _rescue_before_teardown

        await db.init_db()
        run = self._run()
        await db.insert_run(run)
        pushed = dispatch.RescueResult(
            "repo-a",
            attempted=True,
            pushed=True,
            committed=True,
            branch="feat/x-r2",
        )
        with (
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
        ):
            mock_dispatch._executor = None
            mock_dispatch.rescue_abandoned_work = MagicMock(return_value=pushed)
            mock_gtd.post_comment = AsyncMock()
            outcome = await _rescue_before_teardown(
                run, [("repo-a", tmp_path, None)], attribution=None
            )

        assert outcome is not None and outcome.ok
        assert run.branch_name == "feat/x-r2"
        stored = await db.get_run(run.id)
        assert stored is not None and stored.branch_name == "feat/x-r2"
        assert "`feat/x-r2`" in mock_gtd.post_comment.await_args.args[1]


class TestWorkerRecordsFallbackBranch:
    @pytest.mark.asyncio
    async def test_backstop_fallback_is_recorded_everywhere(self, tmp_path) -> None:
        """AC3: db branch_name, verification, completion comment and rescue."""
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import CLAUDE
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id="item-fb",
            project_name="TestProject",
            branch_name="feat/fb",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(run)

        def _status(status: PushStatus, branch: str, remote: str | None):
            return RepoPushStatus(
                repo_name="repos-testproj",
                branch=branch,
                status=status,
                local_sha="deadbeef",
                remote_sha=remote,
                commits_ahead=2,
                dirty=False,
            )

        completed_result = MagicMock()
        completed_result.returncode = 0
        ok = MagicMock()
        ok.returncode = 0

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_gtd.get_item = AsyncMock(
                return_value={"id": "item-fb", "title": "Fix", "project_id": "p"}
            )
            mock_gtd.get_project = AsyncMock(
                return_value={
                    "id": "p",
                    "name": "TestProject",
                    "git_origin": "git@host:repos/testproj",
                }
            )
            mock_gtd.post_comment = AsyncMock()
            mock_gtd.set_item_status = AsyncMock()
            ws = tmp_path / "repos-testproj-fb"
            ws.mkdir()
            seed_build_evidence(ws)
            mock_dispatch.prepare_workspace = MagicMock(return_value=ws)
            mock_dispatch.get_head_sha = MagicMock(return_value="base")
            mock_dispatch.repo_name_from_origin = MagicMock(
                return_value="repos-testproj"
            )
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt = MagicMock(return_value="prompt")
            mock_dispatch.is_zero_commits_run = dispatch.is_zero_commits_run
            mock_dispatch.run_agent = AsyncMock(return_value=completed_result)
            mock_dispatch.verify_pushes = MagicMock(
                side_effect=[
                    [_status(PushStatus.unpushed, "feat/fb", "0ld")],
                    [_status(PushStatus.pushed, "feat/fb-r2", "deadbeef")],
                ]
            )
            mock_dispatch.push_unpushed_repo = MagicMock(
                return_value=dispatch.PushAttempt(ok, "feat/fb-r2", fell_back=True)
            )
            mock_dispatch._executor = None
            stub_rescue(mock_dispatch)
            mock_dispatch.cleanup_workspace = MagicMock()

            await _dispatch_worker(run, 50, CLAUDE, 600)

        stored = await db.get_run(run.id)
        assert stored is not None
        assert stored.status.value == "succeeded"
        assert stored.branch_name == "feat/fb-r2"

        second = mock_dispatch.verify_pushes.call_args_list[1]
        assert second.args[1] == "feat/fb"
        assert second.args[2] == {"repos-testproj": "feat/fb-r2"}

        comments = [str(c.args[1]) for c in mock_gtd.post_comment.call_args_list]
        assert any("finished on branch `feat/fb-r2`" in c for c in comments), comments
        assert any(
            "to `feat/fb-r2`" in c and "`feat/fb` already held" in c for c in comments
        ), comments

        rescue_branches = [
            c.args[2] for c in mock_dispatch.rescue_abandoned_work.call_args_list
        ]
        assert rescue_branches == ["feat/fb-r2"]


class TestReviewerReceivesFallbackBranch:
    @pytest.mark.asyncio
    async def test_reviewer_is_handed_the_r2_branch(self, tmp_path) -> None:
        """AC3: the board row still names feat/x; the build recorded feat/x-r2."""
        from agent_gtd_dispatch import db, main
        from agent_gtd_dispatch.engines import CLAUDE

        await db.init_db()
        build = Run(
            item_id="item-rv",
            project_name="P",
            branch_name="feat/x-r2",
            mode=DispatchMode.BUILD,
        )
        await db.insert_run(build)
        board_row = {
            "id": "board-run",
            "rollout_id": "ro-1",
            "remote_run_id": build.id,
            "feature_branch": "feat/x",
            "status": "success",
            "created_at": "2026-10-19T00:00:00Z",
        }
        workspace = SimpleNamespace(
            root=tmp_path,
            repo_paths={"repo": tmp_path},
            default_branches={"repo": "main"},
            workspace_mode=False,
        )
        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_gtd.get_item = AsyncMock(
                return_value={"id": "item-rv", "title": "X", "commit_type": "fix"}
            )
            mock_gtd.list_runs_for_item = AsyncMock(return_value=[board_row])
            mock_gtd.get_rollout_merge_notes = AsyncMock(return_value=[])
            mock_dispatch.ITEM_COMMIT_TYPES = dispatch.ITEM_COMMIT_TYPES
            mock_dispatch.build_review_prompt = MagicMock(return_value="prompt")
            mock_dispatch.compose_squash_subject = MagicMock(return_value=None)
            mock_dispatch.run_agent = AsyncMock(return_value=MagicMock(returncode=0))
            mock_dispatch.read_review_verdict = MagicMock(return_value=(None, "absent"))

            verdict = await main._launch_reviewer(
                "ro-1",
                "item-rv",
                project={"name": "P"},
                workspace=workspace,
                reviewer_engine=CLAUDE,
                token=None,
                attribution=None,
            )

        assert verdict.branch_name == "feat/x-r2"
        assert mock_dispatch.build_review_prompt.call_args.args[3] == "feat/x-r2"

    @pytest.mark.asyncio
    async def test_board_branch_is_used_when_the_build_ran_elsewhere(self) -> None:
        from agent_gtd_dispatch import db, main

        await db.init_db()
        row = {"remote_run_id": "not-on-this-host", "feature_branch": "feat/x-r2"}
        assert await main._review_branch_name("i", {"title": "t"}, row) == "feat/x-r2"
