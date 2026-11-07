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
from datetime import datetime
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
            mock_dispatch.find_newer_fallback_branch = MagicMock(return_value=None)
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


def _push_ref(repo: Path, branch: str, ref: str = "HEAD") -> None:
    _git(repo, "push", "origin", f"{ref}:refs/heads/{branch}")


class TestFallbackReuse:
    def test_two_pushes_land_on_one_branch(self, tmp_path, caplog) -> None:
        origin = _Origin(tmp_path)
        old_sha = origin.leave_old_run_on("feat/x")
        repo = origin.clone("rebuild")
        _commit(repo, "a.py")
        first = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)
        assert (first.branch, first.fell_back) == ("feat/x-r2", True)
        b = _commit(repo, "b.py")
        with caplog.at_level("INFO"):
            second = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)
        assert (second.branch, second.fell_back) == ("feat/x-r2", True)
        assert _remote_sha(origin.path, "feat/x-r2") == b
        assert _remote_sha(origin.path, "feat/x-r3") == ""
        assert _remote_sha(origin.path, "feat/x") == old_sha
        assert any(
            "mode=reuse" in r.getMessage() and "chosen=feat/x-r2" in r.getMessage()
            for r in caplog.records
        )

    def test_rescue_after_backstop_reuses_r2(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        old_sha = origin.leave_old_run_on("feat/x")
        repo = origin.clone("rebuild")
        _commit(repo, "a.py")
        dispatch.push_unpushed_repo(repo, "feat/x", 60.0)
        (repo / "dirty.txt").write_text("dirty\n")
        result = dispatch.rescue_abandoned_work("repo-a", repo, "feat/x", "run-1")
        assert result.pushed is True
        assert result.branch == "feat/x-r2"
        assert _remote_sha(origin.path, "feat/x-r2") == _git(repo, "rev-parse", "HEAD")
        assert _remote_sha(origin.path, "feat/x-r3") == ""
        assert _remote_sha(origin.path, "feat/x") == old_sha

    def test_foreign_r2_is_skipped_and_r3_reused(self, tmp_path, caplog) -> None:
        origin = _Origin(tmp_path)
        origin.leave_old_run_on("feat/x")
        r2 = origin.leave_old_run_on("feat/x-r2")
        repo = origin.clone("rebuild")
        _commit(repo, "a.py")
        with caplog.at_level("INFO"):
            first = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)
        assert first.branch == "feat/x-r3"
        assert any(
            "mode=first-free" in r.getMessage() and "chosen=feat/x-r3" in r.getMessage()
            for r in caplog.records
        )
        b = _commit(repo, "b.py")
        second = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)
        assert second.branch == "feat/x-r3"
        assert _remote_sha(origin.path, "feat/x-r3") == b
        assert _remote_sha(origin.path, "feat/x-r2") == r2
        assert _remote_sha(origin.path, "feat/x-r4") == ""

    def test_up_to_date_fallback_is_success(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        origin.leave_old_run_on("feat/x")
        repo = origin.clone("rebuild")
        _commit(repo, "a.py")
        dispatch.push_unpushed_repo(repo, "feat/x", 60.0)
        again = dispatch.push_with_fallback(
            repo, "feat/x", no_verify=True, timeout_seconds=60.0
        )
        assert again.proc.returncode == 0
        assert (again.branch, again.fell_back) == ("feat/x-r2", True)
        assert _remote_sha(origin.path, "feat/x-r3") == ""

    def test_fewest_commits_wins(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        origin.leave_old_run_on("feat/x")
        repo = origin.clone("rebuild")
        _git(repo, "push", "origin", "main:refs/heads/feat/x-r2")
        _commit(repo, "a.py")
        _push_ref(repo, "feat/x-r3")
        r2 = _remote_sha(origin.path, "feat/x-r2")
        b = _commit(repo, "b.py")
        attempt = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)
        assert attempt.branch == "feat/x-r3"
        assert _remote_sha(origin.path, "feat/x-r3") == b
        assert _remote_sha(origin.path, "feat/x-r2") == r2

    def test_lone_ancestor_is_reused(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        origin.leave_old_run_on("feat/x")
        repo = origin.clone("rebuild")
        _git(repo, "push", "origin", "main:refs/heads/feat/x-r2")
        head = _commit(repo, "a.py")
        attempt = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)
        assert attempt.branch == "feat/x-r2"
        assert _remote_sha(origin.path, "feat/x-r2") == head

    def test_bad_names_are_ignored(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        origin.leave_old_run_on("feat/x")
        repo = origin.clone("rebuild")
        decoys = ["feat/x-rfoo", "feat/x-r02", "feat/x-r1", "feat/x-r51", "feat/xx-r2"]
        base = _git(repo, "rev-parse", "main")
        for d in decoys:
            _git(repo, "push", "origin", f"main:refs/heads/{d}")
        head = _commit(repo, "a.py")
        attempt = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)
        assert attempt.branch == "feat/x-r2"
        assert _remote_sha(origin.path, "feat/x-r2") == head
        for d in decoys:
            assert _remote_sha(origin.path, d) == base

    def test_glob_probe_failure_returns_none(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        repo = origin.clone("rebuild")
        _commit(repo, "a.py")
        real_run = subprocess.run

        def _fail(cmd, *a, **kw):
            if "ls-remote" in cmd:
                return subprocess.CompletedProcess(cmd, 128, b"", b"boom")
            return real_run(cmd, *a, **kw)

        with patch("agent_gtd_dispatch.dispatch.subprocess.run", side_effect=_fail):
            assert dispatch.fallback_branch_for(repo, "feat/x", 60.0) is None

    def test_glob_probe_timeout_returns_none(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        repo = origin.clone("rebuild")
        real_run = subprocess.run

        def _timeout(cmd, *a, **kw):
            if "ls-remote" in cmd:
                raise subprocess.TimeoutExpired(cmd, 1)
            return real_run(cmd, *a, **kw)

        with patch("agent_gtd_dispatch.dispatch.subprocess.run", side_effect=_timeout):
            assert dispatch.fallback_branch_for(repo, "feat/x", 60.0) is None

    def test_no_chosen_branch_keeps_failed_attempt(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        old_sha = origin.leave_old_run_on("feat/x")
        repo = origin.clone("rebuild")
        _commit(repo, "a.py")
        with patch.object(
            dispatch, "_choose_fallback_branch", return_value=(None, "first-free")
        ):
            attempt = dispatch.push_unpushed_repo(repo, "feat/x", 60.0)
        assert attempt.branch == "feat/x"
        assert attempt.fell_back is False
        assert attempt.proc.returncode != 0
        assert _remote_sha(origin.path, "feat/x-r2") == ""
        assert _remote_sha(origin.path, "feat/x") == old_sha

    def test_merge_base_oserror_is_nonfatal(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        origin.leave_old_run_on("feat/x")
        repo = origin.clone("rebuild")
        _git(repo, "push", "origin", "main:refs/heads/feat/x-r2")
        _commit(repo, "a.py")
        real_run = subprocess.run

        def _boom(cmd, *a, **kw):
            if "merge-base" in cmd:
                raise OSError("nope")
            return real_run(cmd, *a, **kw)

        with patch("agent_gtd_dispatch.dispatch.subprocess.run", side_effect=_boom):
            assert dispatch.fallback_branch_for(repo, "feat/x", 60.0) == "feat/x-r3"

    def test_nested_suffix_warns(self, tmp_path, caplog) -> None:
        origin = _Origin(tmp_path)
        origin.leave_old_run_on("feat/x-r2")
        repo = origin.clone("rebuild", "feat/x-r2")
        _commit(repo, "a.py")
        with caplog.at_level("WARNING"):
            dispatch.push_unpushed_repo(repo, "feat/x-r2", 60.0)
        assert any("nested suffix possible" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# review-branch-source logging and the newer -rN probe
# ---------------------------------------------------------------------------

_OLD = "2026-01-01T00:00:00+00:00"
_NEW = "2026-06-01T00:00:00+00:00"
_STARTED = "2026-03-01T00:00:00+00:00"


def _commit_at(repo: Path, name: str, when: str) -> str:
    (repo / name).write_text(name)
    _git(repo, "add", "-A")
    env = {**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when}
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", name],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )
    return _git(repo, "rev-parse", "HEAD")


def _push_branch_at(origin: _Origin, branch: str, when: str, base: str = "main") -> str:
    name = f"mk-{branch.replace('/', '-')}"
    repo = origin.clone(name, branch)
    if base != "main":
        _git(repo, "reset", "--hard", f"origin/{base}")
    sha = _commit_at(repo, f"{name}.txt", when)
    _git(repo, "push", "origin", f"HEAD:refs/heads/{branch}")
    return sha


def _started() -> datetime:
    return datetime.fromisoformat(_STARTED)


def _probe(origin: _Origin, chosen: str = "feat/x") -> str | None:
    review = origin.clone("review", "scratch")
    return dispatch.find_newer_fallback_branch(review, chosen, _started(), "main", 60)


class TestReviewBranchSourceLog:
    @pytest.mark.asyncio
    async def test_sources_and_disagreement(self, caplog) -> None:
        from agent_gtd_dispatch import db, main

        await db.init_db()
        build = Run(
            item_id="i", project_name="P", branch_name="feat/h", mode=DispatchMode.BUILD
        )
        await db.insert_run(build)
        with caplog.at_level("INFO"):
            await main._review_branch_name(
                "i",
                {"title": "t"},
                {"remote_run_id": "elsewhere", "feature_branch": "feat/b"},
            )
            await main._review_branch_name("i", {"title": "t"}, {})
            got = await main._review_branch_name(
                "i",
                {"title": "t"},
                {"remote_run_id": build.id, "feature_branch": "feat/b"},
            )
            await main._review_branch_name(
                "i",
                {"title": "t"},
                {"remote_run_id": build.id, "feature_branch": "feat/h"},
            )
        assert got == "feat/h"
        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            "review-branch-source" in m and "source=board-row" in m for m in msgs
        )
        assert any(
            "review-branch-source" in m and "source=deterministic" in m for m in msgs
        )
        assert sum("source=host-record" in m for m in msgs) == 2
        disagree = [m for m in msgs if "review-branch-disagree" in m]
        assert len(disagree) == 1
        assert "host=feat/h board=feat/b" in disagree[0]


class TestFindNewerFallbackBranch:
    def test_fast_forward_has_no_candidates(self, tmp_path, caplog) -> None:
        origin = _Origin(tmp_path)
        _push_branch_at(origin, "feat/x", _NEW)
        with caplog.at_level("INFO"):
            assert _probe(origin) is None
        assert "review-branch-probe" not in caplog.text

    def test_fresh_r3_beats_stale_chosen(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        _push_branch_at(origin, "feat/x", _OLD)
        _push_branch_at(origin, "feat/x-r3", _NEW)
        assert _probe(origin) == "feat/x-r3"

    def test_default_advancing_after_cut_still_found(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        _push_branch_at(origin, "feat/x", _OLD)
        _push_branch_at(origin, "feat/x-r3", _NEW)
        adv = origin.clone("adv", "adv")
        _commit_at(adv, "later.txt", _NEW)
        _git(adv, "push", "origin", "HEAD:refs/heads/main")
        assert _probe(origin) == "feat/x-r3"

    def test_rejections(self, tmp_path, caplog) -> None:
        cases = []
        o1 = _Origin(tmp_path / "a") if (tmp_path / "a").mkdir() is None else None
        assert o1 is not None
        _push_branch_at(o1, "feat/x", _NEW)
        _push_branch_at(o1, "feat/x-r3", _NEW)
        cases.append((o1, "chosen-newer"))
        (tmp_path / "b").mkdir()
        o2 = _Origin(tmp_path / "b")
        _push_branch_at(o2, "feat/x", _OLD)
        _push_branch_at(o2, "feat/x-r3", _OLD)
        cases.append((o2, "candidate-older"))
        (tmp_path / "c").mkdir()
        o3 = _Origin(tmp_path / "c")
        _push_branch_at(o3, "feat/x-r2", _NEW)
        # chosen descends from the candidate but is old: rebase the old commit
        # chain so the candidate is an ancestor of chosen.
        repo = o3.clone("chosenwork", "feat/x")
        _git(repo, "reset", "--hard", "origin/feat/x-r2")
        _commit_at(repo, "tip.txt", _OLD)
        _git(repo, "push", "origin", "HEAD:refs/heads/feat/x")
        cases.append((o3, "already-in-chosen"))
        for origin, reason in cases:
            caplog.clear()
            with caplog.at_level("INFO"):
                assert _probe(origin) is None
            assert f"result=none reason={reason}" in caplog.text, reason

    def test_older_r4_skipped_for_fresh_r3(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        _push_branch_at(origin, "feat/x", _OLD)
        _push_branch_at(origin, "feat/x-r3", _NEW)
        _push_branch_at(origin, "feat/x-r4", _OLD)
        assert _probe(origin) == "feat/x-r3"

    def test_rescue_refs_never_chosen(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        _push_branch_at(origin, "feat/x", _OLD)
        _push_branch_at(origin, "feat/x-rescue-abc", _NEW)
        assert _probe(origin) is None

    def test_origin_removed_returns_none_and_logs(self, tmp_path, caplog) -> None:
        import shutil

        origin = _Origin(tmp_path)
        _push_branch_at(origin, "feat/x", _OLD)
        review = origin.clone("review", "scratch")
        shutil.rmtree(origin.path)
        with caplog.at_level("INFO"):
            got = dispatch.find_newer_fallback_branch(
                review, "feat/x", _started(), "main", 60
            )
        assert got is None
        assert "review-branch-probe-failed" in caplog.text

    def test_empty_chosen(self, tmp_path) -> None:
        origin = _Origin(tmp_path)
        assert _probe(origin, "") is None


class TestLaunchReviewerProbe:
    @staticmethod
    async def _launch(workspace, started_at, finder, caplog):
        from agent_gtd_dispatch import db, main
        from agent_gtd_dispatch.engines import CLAUDE

        await db.init_db()
        board_row = {
            "id": "board-run",
            "rollout_id": "ro-1",
            "remote_run_id": "not-here",
            "feature_branch": "feat/x",
            "status": "success",
            "created_at": "2026-10-19T00:00:00Z",
            "started_at": started_at,
        }
        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            caplog.at_level("INFO"),
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
            mock_dispatch.refresh_feature_branch_refs = MagicMock(
                return_value={"repo": "refreshed"}
            )
            mock_dispatch.find_newer_fallback_branch = finder
            mock_dispatch.last_probe_commit_times = dispatch.last_probe_commit_times
            verdict = await main._launch_reviewer(
                "ro-1",
                "item-rv",
                project={"name": "P"},
                workspace=workspace,
                reviewer_engine=CLAUDE,
                token=None,
                attribution=None,
            )
        return verdict, mock_dispatch

    @pytest.mark.asyncio
    @pytest.mark.parametrize("started", [_STARTED, "2026-03-01T00:00:00"])
    async def test_stale_board_branch_switches_to_r3(
        self, tmp_path, caplog, started
    ) -> None:
        origin = _Origin(tmp_path)
        _push_branch_at(origin, "feat/x", _OLD)
        _push_branch_at(origin, "feat/x-r3", _NEW)
        _push_branch_at(origin, "feat/x-rescue-k", _NEW)
        review = origin.clone("review", "scratch")
        ws = SimpleNamespace(
            root=tmp_path,
            repo_paths={"repo": review},
            default_branches={"repo": "main"},
            workspace_mode=False,
        )
        verdict, md = await self._launch(
            ws, started, dispatch.find_newer_fallback_branch, caplog
        )
        assert verdict.branch_name == "feat/x-r3"
        assert md.build_review_prompt.call_args.args[3] == "feat/x-r3"
        assert md.refresh_feature_branch_refs.call_args.args[1] == "feat/x-r3"
        assert "review-branch-mismatch rollout_id=ro-1 item=item-rv" in caplog.text
        assert "chosen=feat/x newer=feat/x-r3" in caplog.text

    @pytest.mark.asyncio
    async def test_no_started_at_skips_probe(self, tmp_path, caplog) -> None:
        finder = MagicMock(return_value="feat/x-r9")
        ws = SimpleNamespace(
            root=tmp_path,
            repo_paths={"repo": tmp_path},
            default_branches={"repo": "main"},
            workspace_mode=False,
        )
        verdict, _ = await self._launch(ws, None, finder, caplog)
        finder.assert_not_called()
        assert verdict.branch_name == "feat/x"

    @pytest.mark.asyncio
    async def test_two_repos_first_wins_and_ambiguity_logged(
        self, tmp_path, caplog
    ) -> None:
        finder = MagicMock(side_effect=["feat/x-r2", "feat/x-r3"])
        ws = SimpleNamespace(
            root=tmp_path,
            repo_paths={"b": tmp_path, "a": tmp_path},
            default_branches={"a": "main", "b": "main"},
            workspace_mode=False,
        )
        verdict, _ = await self._launch(ws, _STARTED, finder, caplog)
        assert verdict.branch_name == "feat/x-r2"
        assert (
            "review-branch-mismatch-ambiguous item=item-rv names=feat/x-r2,feat/x-r3"
            in caplog.text
        )
