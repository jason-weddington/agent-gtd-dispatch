"""Tests for the worker-driven rollout loop and its reviewer.

The resident-manager relaunch ladder these tests used to cover was retired
(item 7baf77d0); see TestStaleUnownedRolloutIsNeverRelaunched at the bottom.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agent_gtd_dispatch.models import Run, RunStatus


def _make_run(mode: str = "manage", rollout_id: str = "rollout-abc") -> Run:
    """Build a minimal Run for testing."""
    return Run(
        item_id="item-123",
        project_name="test-project",
        mode=mode,
        rollout_id=rollout_id,
        engine="claude-code",
    )


@pytest.fixture(autouse=True)
def _env(tmp_path):
    """Set required env vars — mirrors the global fixture in test_api.py."""
    import os
    from unittest.mock import patch as _patch

    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
    }
    with _patch.dict(os.environ, env):
        from agent_gtd_dispatch import config

        config.load()
        yield


# ---------------------------------------------------------------------------
# Worker-driven rollout wave loop
#
# The worker owns the loop; a short-lived REVIEW-mode agent reviews and merges
# one completed build at a time. These tests pin the failure mode that would
# be silent: the advance/in-flight combination (either source alone is a hang).
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _loop_patch_stack():
    """Isolate every module-level dict + collaborator the wave loop touches."""
    with (
        patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
        patch("agent_gtd_dispatch.main.db") as mock_db,
        patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()) as mock_sleep,
        patch("agent_gtd_dispatch.main._active_processes", {}) as active,
        patch("agent_gtd_dispatch.main._rollout_loops", {}) as rollout_loops,
        patch("agent_gtd_dispatch.main._rollout_redispatches", {}) as redispatches,
        patch("agent_gtd_dispatch.main._rollout_unrecorded_merges", {}) as unrecorded,
    ):
        mock_gtd.update_rollout_state = AsyncMock()
        mock_gtd.post_comment = AsyncMock()
        mock_gtd.halt_rollout = AsyncMock()
        mock_gtd.dispatch_item = AsyncMock(return_value={"id": "child-run"})
        mock_gtd.reset_rollout_item = AsyncMock(return_value={})
        mock_gtd.complete_in_rollout = AsyncMock(return_value={"newly_ready": []})
        mock_gtd.get_item = AsyncMock(return_value={"id": "item-a", "title": "t"})
        mock_db.insert_run = AsyncMock()
        mock_db.update_run = AsyncMock()
        # Default: this host HAS a local run row for whatever rollout is under
        # test, i.e. it is resuming its own work. The sweep's ownership check
        # reads this; tests for the not-ours case override it with [].
        mock_db.list_runs_by_rollout = AsyncMock(return_value=[_make_run()])
        yield SimpleNamespace(
            gtd=mock_gtd,
            db=mock_db,
            dispatch=mock_dispatch,
            sleep=mock_sleep,
            active=active,
            rollout_loops=rollout_loops,
            redispatches=redispatches,
            unrecorded=unrecorded,
        )


def _http_error(code):
    req = httpx.Request("POST", "http://gtd.test/x")
    return httpx.HTTPStatusError(
        f"{code}", request=req, response=httpx.Response(code, request=req)
    )


def _fake_workspace():
    from pathlib import Path

    return SimpleNamespace(
        root=Path("/tmp/rollout-ws"),  # noqa: S108 — never touched, dispatch is mocked
        repo_paths={"repo-a": Path("/tmp/rollout-ws/repo-a")},  # noqa: S108
        default_branches={"repo-a": "main"},
        created=False,
        workspace_mode=True,
    )


async def _tick(ctx, **overrides):
    """Run one wave-loop tick with sensible defaults."""
    from agent_gtd_dispatch.main import _rollout_wave_tick

    kwargs = {
        "project": {"name": "p", "workspace_repos": ["git@h:o/repo-a"]},
        "workspace_ref": [_fake_workspace()],
        "reviewer_engine": MagicMock(name="claude-code-sonnet"),
        "dispatch_attempts": {},
        "token": None,
        "attribution": None,
        "loop_run_id": "loop-run-1",
    }
    kwargs.update(overrides)
    return await _rollout_wave_tick("rollout-abc", **kwargs)


class TestAdvanceInFlightCombination:
    """`advance_rollout` alone reports finished items as in-flight forever."""

    def test_terminal_run_is_a_completed_build(self) -> None:
        from agent_gtd_dispatch.main import _completed_build_item_ids

        advance = {"in_progress": ["item-a", "item-b"], "next_ready": []}
        in_flight = [{"runId": "r-b", "itemId": "item-b", "status": "running"}]
        assert _completed_build_item_ids(advance, in_flight) == ["item-a"]

    def test_all_in_flight_yields_nothing_to_review(self) -> None:
        from agent_gtd_dispatch.main import _completed_build_item_ids

        advance = {"in_progress": ["item-a"]}
        in_flight = [{"runId": "r-a", "itemId": "item-a", "status": "pending"}]
        assert _completed_build_item_ids(advance, in_flight) == []

    async def test_loop_advances_when_a_dispatched_run_goes_terminal(self) -> None:
        """A dispatched item whose run has gone terminal gets REVIEWED, not waited on.

        This is the whole trap: `in_progress` still names the item (its
        rollout_items row is `dispatched` and only the worker can change that),
        while `inFlightBuildRuns` — the JOIN against claude_runs.status — no
        longer does. Driven by `advance_rollout` alone the loop would poll
        forever.
        """
        from agent_gtd_dispatch.main import _TICK_CONTINUE, ReviewVerdict

        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._launch_reviewer",
                new=AsyncMock(return_value=ReviewVerdict("skip", "nothing", "", "")),
            ) as mock_reviewer,
        ):
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={
                    "next_ready": [],
                    "in_progress": ["item-a"],
                    "blocked": [],
                    "graph_complete": False,
                }
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            directive = await _tick(ctx)

        assert directive == _TICK_CONTINUE
        mock_reviewer.assert_awaited_once()
        ctx.sleep.assert_not_awaited()

    async def test_loop_waits_while_the_run_is_still_in_flight(self) -> None:
        from agent_gtd_dispatch.main import _TICK_WAIT

        with (
            _loop_patch_stack() as ctx,
            patch("agent_gtd_dispatch.main._launch_reviewer") as mock_reviewer,
        ):
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={
                    "next_ready": [],
                    "in_progress": ["item-a"],
                    "graph_complete": False,
                }
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={
                    "status": "running",
                    "inFlightBuildRuns": [
                        {"runId": "r-a", "itemId": "item-a", "status": "running"}
                    ],
                }
            )
            directive = await _tick(ctx)

        assert directive == _TICK_WAIT
        mock_reviewer.assert_not_called()

    async def test_graph_complete_is_read_not_computed(self) -> None:
        from agent_gtd_dispatch.main import _TICK_DONE

        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={
                    "next_ready": [],
                    "in_progress": [],
                    "graph_complete": True,
                }
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            assert await _tick(ctx) == _TICK_DONE

    async def test_ready_items_are_dispatched(self) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE

        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={
                    "next_ready": ["item-a"],
                    "in_progress": [],
                    "graph_complete": False,
                }
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            assert await _tick(ctx) == _TICK_CONTINUE

        ctx.gtd.dispatch_item.assert_awaited_once_with(
            "item-a", rollout_id="rollout-abc", token=None
        )

    async def test_nothing_ready_nothing_in_flight_halts(self) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED

        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={
                    "next_ready": [],
                    "in_progress": [],
                    "blocked": ["item-z"],
                    "graph_complete": False,
                }
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            assert await _tick(ctx) == _TICK_HALTED

        ctx.gtd.halt_rollout.assert_awaited_once()
        assert "stalled" in ctx.gtd.halt_rollout.await_args.kwargs["reason"]


class TestVerdictHandling:
    """The reviewer decides; the WORKER acts — including any re-dispatch."""

    async def _act(
        self,
        ctx,
        verdict,
        *,
        rationale="because",
        merge_note="note",
        commit_type="",
        commit_type_reason="",
    ):
        from agent_gtd_dispatch.main import ReviewVerdict, _act_on_verdict

        return await _act_on_verdict(
            "rollout-abc",
            "item-a",
            ReviewVerdict(
                verdict, rationale, merge_note, "", commit_type, commit_type_reason
            ),
            token=None,
            attribution=None,
        )

    async def test_merge_records_the_item_complete(self) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE

        with _loop_patch_stack() as ctx:
            ctx.gtd.complete_in_rollout = AsyncMock(
                return_value={"newly_ready": ["item-b"]}
            )
            assert await self._act(ctx, "merge") == _TICK_CONTINUE

        call = ctx.gtd.complete_in_rollout.await_args
        assert call.args == ("rollout-abc", "item-a")
        assert call.kwargs["outcome"] == "completed"
        assert call.kwargs["merge_note"] == "note"
        ctx.gtd.halt_rollout.assert_not_awaited()

    async def test_merge_with_graph_complete_ends_the_loop(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _TICK_DONE

        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, "agent_gtd_dispatch.main"),
        ):
            ctx.gtd.complete_in_rollout = AsyncMock(
                return_value={"newly_ready": [], "graph_complete": True}
            )
            assert await self._act(ctx, "merge") == _TICK_DONE
        ctx.gtd.halt_rollout.assert_not_awaited()
        hits = [r for r in caplog.records if "via=completion" in r.getMessage()]
        assert len(hits) == 1
        assert "item-a" in hits[0].getMessage()

    async def test_skip_with_graph_complete_ends_the_loop(self) -> None:
        from agent_gtd_dispatch.main import _TICK_DONE

        with _loop_patch_stack() as ctx:
            ctx.gtd.complete_in_rollout = AsyncMock(
                return_value={"newly_ready": [], "graph_complete": True}
            )
            assert await self._act(ctx, "skip") == _TICK_DONE

    async def test_graph_complete_false_continues(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE

        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, "agent_gtd_dispatch.main"),
        ):
            ctx.gtd.complete_in_rollout = AsyncMock(
                return_value={"newly_ready": [], "graph_complete": False}
            )
            assert await self._act(ctx, "merge") == _TICK_CONTINUE
        assert not any("via=completion" in r.getMessage() for r in caplog.records)

    async def test_missing_graph_complete_continues(self) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE

        with _loop_patch_stack() as ctx:
            ctx.gtd.complete_in_rollout = AsyncMock(return_value={"newly_ready": []})
            assert await self._act(ctx, "merge") == _TICK_CONTINUE

    async def test_merge_comment_reports_commit_type(self) -> None:
        with _loop_patch_stack() as ctx:
            ctx.gtd.complete_in_rollout = AsyncMock(return_value={})
            await self._act(
                ctx, "merge", commit_type="feat", commit_type_reason="rule 3: new tool"
            )
        text = ctx.gtd.post_comment.await_args.args[1]
        assert "Commit type: `feat` (rule 3: new tool)." in text

    async def test_merge_comment_without_commit_type(self) -> None:
        with _loop_patch_stack() as ctx:
            ctx.gtd.complete_in_rollout = AsyncMock(return_value={})
            await self._act(ctx, "merge")
        text = ctx.gtd.post_comment.await_args.args[1]
        assert "Commit type: not reported by reviewer." in text

    async def test_skip_comment_has_no_commit_type(self) -> None:
        with _loop_patch_stack() as ctx:
            await self._act(ctx, "skip")
        text = ctx.gtd.post_comment.await_args.args[1]
        assert "Commit type" not in text

    async def test_skip_advances_the_wave(self) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE

        with _loop_patch_stack() as ctx:
            assert await self._act(ctx, "skip") == _TICK_CONTINUE

        assert ctx.gtd.complete_in_rollout.await_args.kwargs["outcome"] == "skipped"
        ctx.gtd.halt_rollout.assert_not_awaited()

    async def test_halt_halts_the_rollout(self) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED

        with _loop_patch_stack() as ctx:
            assert await self._act(ctx, "halt", rationale="auth code") == _TICK_HALTED

        ctx.gtd.halt_rollout.assert_awaited_once()
        assert "auth code" in ctx.gtd.halt_rollout.await_args.kwargs["reason"]
        ctx.gtd.complete_in_rollout.assert_not_awaited()

    async def test_redispatch_once_then_halts_at_the_cap(self) -> None:
        from agent_gtd_dispatch.main import (
            _TICK_CONTINUE,
            _TICK_HALTED,
            MAX_ITEM_REDISPATCHES,
        )

        assert MAX_ITEM_REDISPATCHES == 1

        with _loop_patch_stack() as ctx:
            assert await self._act(ctx, "re-dispatch") == _TICK_CONTINUE
            # The reset is what makes the re-dispatch land: dispatch_item's
            # wave linkage is guarded by `AND status = 'ready'`.
            ctx.gtd.reset_rollout_item.assert_awaited_once_with(
                "rollout-abc", "item-a", token=None
            )
            ctx.gtd.dispatch_item.assert_awaited_once()

            # Second `re-dispatch` verdict for the SAME item hits the cap.
            assert await self._act(ctx, "re-dispatch") == _TICK_HALTED
            assert ctx.gtd.dispatch_item.await_count == 1

        ctx.gtd.halt_rollout.assert_awaited_once()
        assert "cap reached" in ctx.gtd.halt_rollout.await_args.kwargs["reason"]


class TestWorkerPublishesRolloutState:
    """The manager_* columns lose their only writer — the worker takes over."""

    async def test_polling_phase_published_while_builds_are_in_flight(self) -> None:
        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={
                    "next_ready": [],
                    "in_progress": ["item-a"],
                    "graph_complete": False,
                }
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={
                    "status": "running",
                    "inFlightBuildRuns": [
                        {"runId": "r-a", "itemId": "item-a", "status": "running"}
                    ],
                }
            )
            await _tick(ctx)

        phases = [c.args[1] for c in ctx.gtd.update_rollout_state.await_args_list]
        assert "polling" in phases

    async def test_dispatching_phase_names_the_item(self) -> None:
        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={
                    "next_ready": ["item-a"],
                    "in_progress": [],
                    "graph_complete": False,
                }
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            await _tick(ctx)

        call = ctx.gtd.update_rollout_state.await_args
        assert call.args[1] == "dispatching"
        assert call.kwargs["current_item_id"] == "item-a"

    async def test_halt_publishes_the_halted_phase(self) -> None:
        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={
                    "next_ready": [],
                    "in_progress": [],
                    "graph_complete": False,
                }
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            await _tick(ctx)

        phases = [c.args[1] for c in ctx.gtd.update_rollout_state.await_args_list]
        assert "halted" in phases

    async def test_reviewing_phase_published_before_the_reviewer_launches(
        self,
    ) -> None:
        from agent_gtd_dispatch.main import ReviewVerdict

        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._launch_reviewer",
                new=AsyncMock(return_value=ReviewVerdict("skip", "x", "", "")),
            ),
        ):
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={
                    "next_ready": [],
                    "in_progress": ["item-a"],
                    "graph_complete": False,
                }
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            await _tick(ctx)

        phases = [c.args[1] for c in ctx.gtd.update_rollout_state.await_args_list]
        assert "reviewing" in phases


class TestReviewerRunMode:
    """A reviewer run carries its own dispatch mode, never MANAGE."""

    def test_reviewer_run_is_not_manage_mode(self) -> None:
        from agent_gtd_dispatch.models import DispatchMode, Run

        reviewer = Run(
            item_id="item-a",
            project_name="p",
            mode=DispatchMode.REVIEW,
            rollout_id="rollout-abc",
        )
        assert reviewer.mode != DispatchMode.MANAGE
        assert reviewer.mode == "review"


class TestReviewerEnvWithholdsDispatchKeys:
    """A reviewer returns a verdict; it never dispatches. So: no dispatch keys."""

    def test_review_env_contains_neither_dispatch_key(self) -> None:
        import os

        from agent_gtd_dispatch.engines import CLAUDE_SONNET, build_env
        from agent_gtd_dispatch.models import DispatchMode

        with patch.dict(
            os.environ,
            {
                "DISPATCH_LOCAL_URL": "http://localhost:8100",
                "DISPATCH_API_KEY": "super-secret",
                "ANTHROPIC_API_KEY": "sk-ant-nope",
            },
        ):
            env = build_env(CLAUDE_SONNET, mode=DispatchMode.REVIEW)
            manage_env = build_env(CLAUDE_SONNET, mode=DispatchMode.MANAGE)

        assert "DISPATCH_LOCAL_URL" not in env
        assert "DISPATCH_API_KEY" not in env
        # kb-01512: unconditional, for every claude-code subprocess.
        assert "ANTHROPIC_API_KEY" not in env
        # The manage grant is untouched — this is a withholding, not a removal.
        assert "DISPATCH_LOCAL_URL" in manage_env
        assert "DISPATCH_API_KEY" in manage_env


class TestReadoptionSweep:
    """A deploy restart must not silently stop every running rollout."""

    async def test_sweep_resumes_a_running_rollout(self) -> None:
        from agent_gtd_dispatch.main import _readopt_running_rollouts

        with (
            _loop_patch_stack() as ctx,
            patch("agent_gtd_dispatch.main._start_rollout_loop") as mock_loop,
        ):
            ctx.gtd.list_running_rollouts = AsyncMock(
                return_value=[{"id": "rollout-abc", "project_name": "p"}]
            )
            await _readopt_running_rollouts()

        mock_loop.assert_called_once()
        resumed = mock_loop.call_args.args[0]
        assert resumed.rollout_id == "rollout-abc"
        assert resumed.mode == "manage"

    async def test_sweep_skips_a_rollout_a_live_loop_already_owns(self) -> None:
        from agent_gtd_dispatch.main import _readopt_running_rollouts

        with (
            _loop_patch_stack() as ctx,
            patch("agent_gtd_dispatch.main._start_rollout_loop") as mock_loop,
        ):
            ctx.rollout_loops["rollout-abc"] = _make_run()
            ctx.gtd.list_running_rollouts = AsyncMock(
                return_value=[{"id": "rollout-abc", "project_name": "p"}]
            )
            await _readopt_running_rollouts()

        mock_loop.assert_not_called()

    async def test_sweep_does_not_adopt_another_hosts_rollout(self) -> None:
        """A rollout this host has no local run row for belongs to someone else.

        ``list_running_rollouts`` is fleet-wide, and the in-process
        ``_rollout_loops`` guard is empty at process start — which is exactly
        when this sweep runs. Without an ownership check every host adopts every
        running rollout on every restart, and a manager owns the squash-merge and
        the push, so that is two processes merging one wave. Caught in production
        when a newly provisioned fourth host came up and immediately started
        driving another project's in-flight rollout.
        """
        from agent_gtd_dispatch.main import _readopt_running_rollouts

        with (
            _loop_patch_stack() as ctx,
            patch("agent_gtd_dispatch.main._start_rollout_loop") as mock_loop,
        ):
            ctx.db.list_runs_by_rollout = AsyncMock(return_value=[])
            ctx.gtd.list_running_rollouts = AsyncMock(
                return_value=[{"id": "someone-elses-rollout", "project_name": "p"}]
            )
            await _readopt_running_rollouts()

        mock_loop.assert_not_called()
        ctx.db.insert_run.assert_not_called()

    async def test_readopt_skips_rollout_where_this_host_only_ran_a_child_build(
        self,
    ) -> None:
        """A child build row is not ownership of the loop.

        Child build runs carry the rollout_id too. A host that built one item
        of a rollout whose loop lives on another host must not resume that loop
        on restart, or two loops drive one rollout (rollout d79f2243, 2026-10-11).
        """
        from agent_gtd_dispatch.main import _readopt_running_rollouts

        with (
            _loop_patch_stack() as ctx,
            patch("agent_gtd_dispatch.main._start_rollout_loop") as mock_loop,
        ):
            ctx.db.list_runs_by_rollout = AsyncMock(
                return_value=[_make_run(mode="build", rollout_id="other-hosts-loop")]
            )
            ctx.gtd.list_running_rollouts = AsyncMock(
                return_value=[{"id": "other-hosts-loop", "project_name": "p"}]
            )
            await _readopt_running_rollouts()

        mock_loop.assert_not_called()
        ctx.db.insert_run.assert_not_called()

    async def test_resumed_loop_does_not_double_dispatch_an_in_flight_item(
        self,
    ) -> None:
        """The frontier is re-READ, so an item already building is never re-dispatched.

        `advance_rollout` reports a `dispatched` item as `in_progress`, never
        as `next_ready`, and the GTD-side wave linkage is additionally guarded
        by `AND status = 'ready'`. Between them a resumed loop cannot
        double-dispatch.
        """
        from agent_gtd_dispatch.main import _TICK_WAIT

        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={
                    "next_ready": [],
                    "in_progress": ["item-a"],
                    "graph_complete": False,
                }
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={
                    "status": "running",
                    "inFlightBuildRuns": [
                        {"runId": "r-a", "itemId": "item-a", "status": "running"}
                    ],
                }
            )
            assert await _tick(ctx) == _TICK_WAIT

        ctx.gtd.dispatch_item.assert_not_awaited()


class TestLaunchReviewer:
    """One reviewer, one build, one bounded verdict."""

    @staticmethod
    def _item() -> dict:
        return {"id": "item-a", "title": "Do it", "acceptance_criteria": ["works"]}

    async def _launch(
        self,
        ctx,
        *,
        verdict_blob,
        reason,
        exit_code=0,
        raises=None,
        item=None,
        prefetched=None,
        workspace=None,
        **extra,
    ):
        from agent_gtd_dispatch.main import _launch_reviewer

        if prefetched is not None:
            extra["item"] = prefetched

        ctx.gtd.get_item = AsyncMock(return_value=item or self._item())
        ctx.gtd.list_runs_for_item = AsyncMock(
            return_value=[
                {
                    "id": "run-a",
                    "rollout_id": "rollout-abc",
                    "feature_branch": "feat/item-a",
                    "status": "success",
                    "error_msg": "",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ]
        )
        ctx.gtd.get_rollout_merge_notes = AsyncMock(return_value=[])
        ctx.dispatch.MERGE_NOTE_CONTEXT_LIMIT = 10
        ctx.dispatch.REVIEW_RATIONALE_MAX_CHARS = 2000
        from agent_gtd_dispatch import dispatch as real_dispatch

        ctx.dispatch.COMMIT_TYPES = real_dispatch.COMMIT_TYPES
        ctx.dispatch.ITEM_COMMIT_TYPES = real_dispatch.ITEM_COMMIT_TYPES
        ctx.dispatch.compose_squash_subject = real_dispatch.compose_squash_subject
        ctx.dispatch.build_review_prompt = MagicMock(return_value="PROMPT")
        ctx.dispatch.clear_review_verdict = MagicMock()
        ctx.dispatch.read_review_verdict = MagicMock(
            return_value=(verdict_blob, reason)
        )
        if raises is not None:
            ctx.dispatch.run_agent = AsyncMock(side_effect=raises)
        else:
            ctx.dispatch.run_agent = AsyncMock(
                return_value=SimpleNamespace(returncode=exit_code)
            )
        return await _launch_reviewer(
            "rollout-abc",
            "item-a",
            project={"name": "P"},
            workspace=workspace or _fake_workspace(),
            reviewer_engine=SimpleNamespace(name="claude-code-sonnet"),
            token=None,
            attribution=None,
            **extra,
        )

    async def test_branch_name_and_repo_states_are_carried(self) -> None:
        with _loop_patch_stack() as ctx:
            outcome = await self._launch(
                ctx,
                verdict_blob={
                    "schema_version": 1,
                    "verdict": "merge",
                    "rationale": "ok",
                    "merge_note": "n",
                    "repo_states": {"repo-a": "committed"},
                },
                reason="ok",
            )
        assert outcome.branch_name == "feat/item-a"
        assert outcome.repo_states == (("repo-a", "committed"),)
        kwargs = ctx.dispatch.build_review_prompt.call_args.kwargs
        assert kwargs["gate_failure_output"] is None

    async def test_resolved_conflict_paths_are_carried(self) -> None:
        import json

        with _loop_patch_stack() as ctx:
            outcome = await self._launch(
                ctx,
                verdict_blob={
                    "schema_version": 1,
                    "verdict": "merge",
                    "rationale": "ok",
                    "merge_note": "n",
                    "resolved_conflict_paths": ["README.md"],
                },
                reason="ok",
            )
        assert outcome.verdict == "merge"
        assert outcome.resolved_conflict_paths == ("README.md",)
        assert outcome.detail.endswith(" [resolved doc conflicts: README.md]")
        completion = json.loads(ctx.db.update_run.await_args.kwargs["completion"])
        assert completion["resolved_conflict_paths"] == ["README.md"]

    async def test_malformed_resolved_conflict_paths_halts_a_merge(self) -> None:
        import json

        with _loop_patch_stack() as ctx:
            outcome = await self._launch(
                ctx,
                verdict_blob={
                    "schema_version": 1,
                    "verdict": "merge",
                    "rationale": "ok",
                    "merge_note": "n",
                    "resolved_conflict_paths": "README.md",
                },
                reason="ok",
            )
        assert outcome.verdict == "halt"
        assert outcome.rationale == (
            "reviewer returned a malformed resolved_conflict_paths"
        )
        assert outcome.detail == "malformed resolved_conflict_paths: 'README.md'"
        assert outcome.resolved_conflict_paths == ()
        kwargs = ctx.db.update_run.await_args.kwargs
        assert kwargs["status"].value == "succeeded"
        assert json.loads(kwargs["completion"])["verdict"] == "halt"

    async def test_malformed_resolved_conflict_paths_ignored_off_merge(self) -> None:
        with _loop_patch_stack() as ctx:
            outcome = await self._launch(
                ctx,
                verdict_blob={
                    "schema_version": 1,
                    "verdict": "skip",
                    "rationale": "not needed",
                    "resolved_conflict_paths": "x",
                },
                reason="ok",
            )
        assert outcome.verdict == "skip"
        assert outcome.resolved_conflict_paths == ()

    async def test_empty_resolved_conflict_paths_leaves_completion_unchanged(
        self,
    ) -> None:
        import json

        with _loop_patch_stack() as ctx:
            outcome = await self._launch(
                ctx,
                verdict_blob={
                    "schema_version": 1,
                    "verdict": "merge",
                    "rationale": "ok",
                    "merge_note": "n",
                    "resolved_conflict_paths": [],
                },
                reason="ok",
            )
        assert outcome.verdict == "merge"
        assert outcome.resolved_conflict_paths == ()
        completion = json.loads(ctx.db.update_run.await_args.kwargs["completion"])
        assert "resolved_conflict_paths" not in completion

    async def test_gate_failure_output_reaches_the_prompt(self) -> None:
        with _loop_patch_stack() as ctx:
            await self._launch(
                ctx,
                verdict_blob={"schema_version": 1, "verdict": "halt", "rationale": "x"},
                reason="ok",
                gate_failure_output="GATE-TAIL",
            )
        kwargs = ctx.dispatch.build_review_prompt.call_args.kwargs
        assert kwargs["gate_failure_output"] == "GATE-TAIL"
        ctx.dispatch.clear_review_verdict.assert_called_once()

    async def test_no_verdict_still_carries_the_branch(self) -> None:
        with _loop_patch_stack() as ctx:
            outcome = await self._launch(ctx, verdict_blob=None, reason="absent")
        assert outcome.verdict == "halt"
        assert outcome.branch_name == "feat/item-a"

    async def test_valid_verdict_is_returned_and_the_run_succeeds(self) -> None:
        with _loop_patch_stack() as ctx:
            outcome = await self._launch(
                ctx,
                verdict_blob={
                    "schema_version": 1,
                    "verdict": "merge",
                    "rationale": "ACs satisfied",
                    "merge_note": "no signature changes",
                    "repo_states": {"repo-a": "merged"},
                },
                reason="ok",
            )

        assert outcome.verdict == "merge"
        assert outcome.merge_note == "no signature changes"
        assert "repo-a=merged" in outcome.detail
        # The reviewer run is REVIEW mode, so the ladder can never see it.
        inserted = ctx.db.insert_run.await_args.args[0]
        assert inserted.mode == "review"
        assert inserted.rollout_id == "rollout-abc"
        # A stale verdict from the previous item is cleared before launch.
        ctx.dispatch.clear_review_verdict.assert_called_once()

    async def test_commit_type_round_trips(self) -> None:
        with _loop_patch_stack() as ctx:
            outcome = await self._launch(
                ctx,
                verdict_blob={
                    "schema_version": 1,
                    "verdict": "merge",
                    "rationale": "ok",
                    "merge_note": "n",
                    "commit_type": "feat",
                    "commit_type_reason": "rule 3: adds a tool",
                },
                reason="ok",
            )
        assert outcome.commit_type == "feat"
        assert outcome.commit_type_reason == "rule 3: adds a tool"

    async def test_invalid_commit_type_is_blanked_and_warned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.main"),
        ):
            outcome = await self._launch(
                ctx,
                verdict_blob={
                    "schema_version": 1,
                    "verdict": "merge",
                    "rationale": "ok",
                    "merge_note": "n",
                    "commit_type": "banana",
                },
                reason="ok",
            )
        assert outcome.verdict == "merge"
        assert outcome.commit_type == ""
        assert any(
            r.levelno == logging.WARNING and "banana" in r.getMessage()
            for r in caplog.records
        )

    _MERGE_BLOB = {  # noqa: RUF012 — read-only test constant
        "schema_version": 1,
        "verdict": "merge",
        "rationale": "ok",
        "merge_note": "n",
        "commit_type": "fix",
        "commit_type_reason": "groomed on the item",
    }

    async def test_unknown_item_commit_type_warns_and_is_blanked(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        item = {**self._item(), "commit_type": "build"}
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            outcome = await self._launch(
                ctx, verdict_blob=dict(self._MERGE_BLOB), reason="ok", item=item
            )
        assert outcome.item_commit_type == ""
        unknown = [
            r
            for r in caplog.records
            if "rollout-loop: commit-type-unknown" in r.getMessage()
        ]
        assert len(unknown) == 1
        assert unknown[0].levelno == logging.WARNING
        assert "value='build'" in unknown[0].getMessage()
        prompt_lines = [
            r.getMessage()
            for r in caplog.records
            if "rollout-loop: review-prompt" in r.getMessage()
        ]
        assert len(prompt_lines) == 1
        assert "commit_type_mode=derived subject=None" in prompt_lines[0]

    async def test_groomed_item_commit_type_is_carried_and_pinned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        item = {**self._item(), "commit_type": "fix"}
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            outcome = await self._launch(
                ctx, verdict_blob=dict(self._MERGE_BLOB), reason="ok", item=item
            )
        assert outcome.item_commit_type == "fix"
        prompt_lines = [
            r for r in caplog.records if "rollout-loop: review-prompt" in r.getMessage()
        ]
        assert len(prompt_lines) == 1
        assert prompt_lines[0].levelno == logging.INFO
        assert (
            "commit_type_mode=pinned subject='fix(item-a): Do it'"
            in prompt_lines[0].getMessage()
        )
        assert not any("commit-type-unknown" in r.getMessage() for r in caplog.records)

    async def test_halt_path_keeps_item_commit_type_default(self) -> None:
        item = {**self._item(), "commit_type": "fix"}
        with _loop_patch_stack() as ctx:
            outcome = await self._launch(
                ctx, verdict_blob=None, reason="absent", item=item
            )
        assert outcome.verdict == "halt"
        assert outcome.item_commit_type == ""

    async def test_reviewer_launched_with_disallowed_tools(self) -> None:
        from agent_gtd_dispatch import dispatch as real_dispatch

        with _loop_patch_stack() as ctx:
            ctx.dispatch.REVIEWER_DISALLOWED_TOOLS = (
                real_dispatch.REVIEWER_DISALLOWED_TOOLS
            )
            await self._launch(ctx, verdict_blob=None, reason="absent")
            kwargs = ctx.dispatch.run_agent.await_args.kwargs

        assert kwargs["disallowed_tools"] == list(
            real_dispatch.REVIEWER_DISALLOWED_TOOLS
        )

    async def test_no_verdict_artifact_is_treated_as_halt(self) -> None:
        with _loop_patch_stack() as ctx:
            outcome = await self._launch(ctx, verdict_blob=None, reason="absent")

        assert outcome.verdict == "halt"
        assert "absent" in outcome.detail
        assert ctx.db.update_run.await_args.kwargs["status"].value == "failed"

    async def _no_verdict_detail(self, tmp_path, transcript: str | None) -> str:
        if transcript is not None:
            (tmp_path / "transcript.txt").write_text(transcript)
        with _loop_patch_stack() as ctx:
            outcome = await self._launch(
                ctx,
                verdict_blob=None,
                reason="absent",
                exit_code=1,
                workspace=SimpleNamespace(
                    **{**vars(_fake_workspace()), "root": tmp_path}
                ),
            )
            err = ctx.db.update_run.await_args.kwargs["error"]
        assert outcome.detail in err
        return outcome.detail

    async def test_no_verdict_reports_envelope_result_text(self, tmp_path) -> None:
        import json

        msg = "You've hit your weekly limit · resets 12am (America/New_York)"
        env = json.dumps({"type": "result", "is_error": True, "result": msg})
        detail = await self._no_verdict_detail(tmp_path, env + "\n")
        assert "verdict artifact absent (reviewer exit=1)" in detail
        assert f"; agent said: {msg}" in detail

    async def test_no_verdict_reports_plain_output_tail(self, tmp_path) -> None:
        detail = await self._no_verdict_detail(tmp_path, "boom: plain failure\n")
        assert detail.endswith("; agent said: boom: plain failure")

    async def test_no_verdict_without_output_is_unchanged(self, tmp_path) -> None:
        detail = await self._no_verdict_detail(tmp_path, None)
        assert detail == "verdict artifact absent (reviewer exit=1)"

    async def test_no_verdict_result_text_truncated_to_300(self, tmp_path) -> None:
        import json

        env = json.dumps({"type": "result", "is_error": True, "result": "x" * 500})
        detail = await self._no_verdict_detail(tmp_path, env)
        assert detail.endswith("; agent said: " + "x" * 300)

    async def test_subprocess_failure_is_treated_as_halt(self) -> None:
        with _loop_patch_stack() as ctx:
            outcome = await self._launch(
                ctx,
                verdict_blob=None,
                reason="absent",
                raises=RuntimeError("boom"),
            )

        assert outcome.verdict == "halt"
        assert "RuntimeError" in outcome.detail

    async def test_reviewer_runs_in_review_mode_with_the_review_budget(self) -> None:
        from agent_gtd_dispatch import config as cfg
        from agent_gtd_dispatch.models import DispatchMode

        with _loop_patch_stack() as ctx:
            await self._launch(
                ctx,
                verdict_blob={
                    "schema_version": 1,
                    "verdict": "skip",
                    "rationale": "empty branch",
                },
                reason="ok",
            )

        kwargs = ctx.dispatch.run_agent.await_args.kwargs
        assert kwargs["mode"] == DispatchMode.REVIEW
        assert kwargs["timeout_seconds"] == cfg.REVIEW_TIMEOUT_SECONDS
        assert ctx.dispatch.run_agent.await_args.args[4] == cfg.REVIEW_MAX_TURNS


class TestGateResultSummary:
    def test_empty_error_msg_reads_as_no_recorded_failure(self) -> None:
        from agent_gtd_dispatch.main import _gate_result_summary

        assert "no failure recorded" in _gate_result_summary({"error_msg": ""})

    def test_gate_failure_is_quoted(self) -> None:
        from agent_gtd_dispatch.main import _gate_result_summary

        summary = _gate_result_summary({"error_msg": "post-run gate failed: ruff"})
        assert "post-run gate failed: ruff" in summary


class TestLatestRunForItem:
    async def test_picks_the_newest_run_for_this_rollout(self) -> None:
        from agent_gtd_dispatch.main import _latest_run_for_item

        with _loop_patch_stack() as ctx:
            ctx.gtd.list_runs_for_item = AsyncMock(
                return_value=[
                    {"id": "old", "rollout_id": "rollout-abc", "created_at": "1"},
                    {"id": "other", "rollout_id": "rollout-zzz", "created_at": "9"},
                    {"id": "new", "rollout_id": "rollout-abc", "created_at": "2"},
                ]
            )
            row = await _latest_run_for_item("item-a", "rollout-abc", token=None)
        assert row["id"] == "new"

    async def test_no_matching_run_returns_empty(self) -> None:
        from agent_gtd_dispatch.main import _latest_run_for_item

        with _loop_patch_stack() as ctx:
            ctx.gtd.list_runs_for_item = AsyncMock(return_value=[])
            assert await _latest_run_for_item("item-a", "r", token=None) == {}

    async def test_lookup_failure_is_not_fatal(self) -> None:
        from agent_gtd_dispatch.main import _latest_run_for_item

        with _loop_patch_stack() as ctx:
            ctx.gtd.list_runs_for_item = AsyncMock(side_effect=RuntimeError("x"))
            assert await _latest_run_for_item("item-a", "r", token=None) == {}


class TestDriveRollout:
    """End-to-end over the loop driver, with the tick stubbed."""

    @staticmethod
    def _run() -> Run:
        return Run(
            project_name="p",
            mode="manage",
            rollout_id="rollout-abc",
            engine="claude-code",
        )

    async def _drive(self, ctx, directives, *, rollout=None):
        from agent_gtd_dispatch.main import _drive_rollout

        ctx.gtd.get_rollout = AsyncMock(
            return_value=rollout or {"project_id": "proj-1"}
        )
        ctx.gtd.get_project = AsyncMock(
            return_value={"name": "P", "workspace_repos": []}
        )
        with patch(
            "agent_gtd_dispatch.main._rollout_wave_tick",
            new=AsyncMock(side_effect=directives),
        ) as tick:
            result = await _drive_rollout(self._run())
        return result, tick

    async def test_waits_then_completes(self) -> None:
        from agent_gtd_dispatch.main import _TICK_DONE, _TICK_WAIT

        with _loop_patch_stack() as ctx:
            result, tick = await self._drive(ctx, [_TICK_WAIT, _TICK_DONE])

        assert result == _TICK_DONE
        assert tick.await_count == 2
        ctx.sleep.assert_awaited_once()

    async def test_halted_stops_the_loop(self) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED

        with _loop_patch_stack() as ctx:
            result, tick = await self._drive(ctx, [_TICK_HALTED])

        assert result == _TICK_HALTED
        assert tick.await_count == 1

    async def test_reviewer_engine_comes_off_the_rollout_row(self) -> None:
        from agent_gtd_dispatch.main import _TICK_DONE

        with _loop_patch_stack() as ctx:
            await self._drive(
                ctx,
                [_TICK_DONE],
                rollout={
                    "project_id": "proj-1",
                    "reviewer_engine": "claude-code-haiku",
                },
            )
            _, tick = await self._drive(
                ctx,
                [_TICK_DONE],
                rollout={
                    "project_id": "proj-1",
                    "reviewer_engine": "claude-code-haiku",
                },
            )
        assert tick.await_args.kwargs["reviewer_engine"].name == "claude-code-haiku"

    async def test_unknown_reviewer_engine_falls_back_to_the_default(self) -> None:
        from agent_gtd_dispatch import config as cfg
        from agent_gtd_dispatch.main import _TICK_DONE

        with _loop_patch_stack() as ctx:
            _, tick = await self._drive(
                ctx,
                [_TICK_DONE],
                rollout={"project_id": "proj-1", "reviewer_engine": "nope-9000"},
            )
        assert tick.await_args.kwargs["reviewer_engine"].name == cfg.REVIEWER_ENGINE


class TestRolloutLoopWorker:
    @staticmethod
    def _run() -> Run:
        return Run(
            project_name="p",
            mode="manage",
            rollout_id="rollout-abc",
            engine="claude-code",
        )

    async def test_done_records_the_run_succeeded_and_frees_the_slot(self) -> None:
        from agent_gtd_dispatch.main import _TICK_DONE, _rollout_loop_worker

        run = self._run()
        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._drive_rollout",
                new=AsyncMock(return_value=_TICK_DONE),
            ),
        ):
            ctx.active[run.id] = MagicMock()
            await _rollout_loop_worker(run)

        statuses = [c.kwargs.get("status") for c in ctx.db.update_run.await_args_list]
        assert statuses[-1].value == "succeeded"
        assert run.id not in ctx.active
        assert "rollout-abc" not in ctx.rollout_loops

    async def test_halted_records_the_run_failed(self) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED, _rollout_loop_worker

        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._drive_rollout",
                new=AsyncMock(return_value=_TICK_HALTED),
            ),
        ):
            await _rollout_loop_worker(self._run())

        assert ctx.db.update_run.await_args.kwargs["status"].value == "failed"

    async def test_a_crashing_loop_is_recorded_not_swallowed(self) -> None:
        from agent_gtd_dispatch.main import _rollout_loop_worker

        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._drive_rollout",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            await _rollout_loop_worker(self._run())

        kwargs = ctx.db.update_run.await_args.kwargs
        assert kwargs["status"].value == "failed"
        assert "rollout_loop_error" in kwargs["error"]

    async def test_cancellation_records_cancelled_and_re_raises(self) -> None:
        from agent_gtd_dispatch.main import _rollout_loop_worker

        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._drive_rollout",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await _rollout_loop_worker(self._run())

        assert ctx.db.update_run.await_args.kwargs["status"].value == "cancelled"

    async def test_graph_complete_via_completion_ends_succeeded(self) -> None:
        """Real _drive_rollout/_rollout_wave_tick: no trailing advance after done."""
        from agent_gtd_dispatch.main import ReviewVerdict, _rollout_loop_worker

        verdict = ReviewVerdict("merge", "r", "note", "")
        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._launch_reviewer",
                new=AsyncMock(return_value=verdict),
            ),
            patch(
                "agent_gtd_dispatch.main._land_reviewed_merge",
                new=AsyncMock(return_value=verdict),
            ),
            patch(
                "agent_gtd_dispatch.main._verify_landed_commit_type", new=AsyncMock()
            ),
        ):
            ctx.dispatch.prepare_rollout_workspace = MagicMock(
                return_value=_fake_workspace()
            )
            ctx.dispatch.land_origin_shas = MagicMock(return_value={"repo-a": "abc"})
            ctx.gtd.get_project = AsyncMock(
                return_value={"name": "p", "workspace_repos": ["git@h:o/repo-a"]}
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={
                    "project_id": "proj-1",
                    "status": "running",
                    "inFlightBuildRuns": [],
                }
            )
            ctx.gtd.advance_rollout = AsyncMock(
                side_effect=[
                    {
                        "next_ready": [],
                        "in_progress": ["item-a"],
                        "blocked": [],
                        "graph_complete": False,
                    },
                    _http_error(422),
                    _http_error(422),
                ]
            )
            ctx.gtd.complete_in_rollout = AsyncMock(
                return_value={"newly_ready": [], "graph_complete": True}
            )
            await _rollout_loop_worker(self._run())

        assert ctx.gtd.advance_rollout.await_count == 1
        kw = ctx.db.update_run.await_args.kwargs
        assert kw["status"] == RunStatus.succeeded
        assert kw["error"] == ""
        ctx.gtd.halt_rollout.assert_not_awaited()
        assert ctx.dispatch.cleanup_workspace.call_count == 1


class TestPublishRolloutPhase:
    async def test_a_publish_failure_never_stops_the_wave(self) -> None:
        from agent_gtd_dispatch.main import _publish_rollout_phase

        with _loop_patch_stack() as ctx:
            ctx.gtd.update_rollout_state = AsyncMock(side_effect=RuntimeError("down"))
            await _publish_rollout_phase("rollout-abc", "polling")  # must not raise


class TestDispatchAttemptGuard:
    async def test_an_item_that_never_leaves_ready_halts_rather_than_looping(
        self,
    ) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE, _TICK_HALTED

        attempts: dict[str, int] = {}
        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={
                    "next_ready": ["item-a"],
                    "in_progress": [],
                    "graph_complete": False,
                }
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            assert await _tick(ctx, dispatch_attempts=attempts) == _TICK_CONTINUE
            assert await _tick(ctx, dispatch_attempts=attempts) == _TICK_CONTINUE
            assert await _tick(ctx, dispatch_attempts=attempts) == _TICK_HALTED

        assert "never" in ctx.gtd.halt_rollout.await_args.kwargs["reason"]


class TestRolloutLeftRunning:
    async def test_a_halted_rollout_stops_the_loop(self) -> None:
        """Status flips between the advance call and the get_rollout read."""
        from agent_gtd_dispatch.main import _TICK_HALTED

        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={"next_ready": [], "in_progress": []}
            )
            ctx.gtd.get_rollout = AsyncMock(return_value={"status": "halted"})
            assert await _tick(ctx) == _TICK_HALTED
        ctx.gtd.halt_rollout.assert_not_awaited()

    async def test_a_completed_rollout_stops_the_loop(self) -> None:
        """Status flips between the advance call and the get_rollout read."""
        from agent_gtd_dispatch.main import _TICK_DONE

        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={"next_ready": [], "in_progress": []}
            )
            ctx.gtd.get_rollout = AsyncMock(return_value={"status": "completed"})
            assert await _tick(ctx) == _TICK_DONE

    async def test_advance_422_on_completed_rollout_is_done(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _TICK_DONE

        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.WARNING, "agent_gtd_dispatch.main"),
        ):
            ctx.gtd.advance_rollout = AsyncMock(side_effect=_http_error(422))
            ctx.gtd.get_rollout = AsyncMock(return_value={"status": "completed"})
            assert await _tick(ctx) == _TICK_DONE
        assert ctx.gtd.get_rollout.await_count == 1
        ctx.gtd.halt_rollout.assert_not_awaited()
        msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "advance-422-reread" in m and "rollout-abc" in m and "action=done" in m
            for m in msgs
        )

    async def test_advance_422_on_halted_rollout_is_halted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED

        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.WARNING, "agent_gtd_dispatch.main"),
        ):
            ctx.gtd.advance_rollout = AsyncMock(side_effect=_http_error(422))
            ctx.gtd.get_rollout = AsyncMock(return_value={"status": "halted"})
            assert await _tick(ctx) == _TICK_HALTED
        assert ctx.gtd.get_rollout.await_count == 1
        ctx.gtd.halt_rollout.assert_not_awaited()
        assert any(
            "advance-422-reread" in r.getMessage() and "action=halted" in r.getMessage()
            for r in caplog.records
        )

    async def test_advance_422_on_running_rollout_reraises(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        err = _http_error(422)
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.WARNING, "agent_gtd_dispatch.main"),
        ):
            ctx.gtd.advance_rollout = AsyncMock(side_effect=err)
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            with pytest.raises(httpx.HTTPStatusError) as ei:
                await _tick(ctx)
        assert ei.value is err
        assert ctx.gtd.get_rollout.await_count == 1
        assert any("action=reraise" in r.getMessage() for r in caplog.records)

    async def test_advance_500_is_not_reread(self) -> None:
        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(side_effect=_http_error(500))
            ctx.gtd.get_rollout = AsyncMock()
            with pytest.raises(httpx.HTTPStatusError):
                await _tick(ctx)
        ctx.gtd.get_rollout.assert_not_awaited()

    async def test_advance_422_with_failing_reread_propagates(self) -> None:
        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(side_effect=_http_error(422))
            ctx.gtd.get_rollout = AsyncMock(side_effect=_http_error(503))
            with pytest.raises(httpx.HTTPStatusError) as ei:
                await _tick(ctx)
        assert ei.value.response.status_code == 503


# ---------------------------------------------------------------------------
# Post-merge commit-type verification (never halts, never changes the outcome)
# ---------------------------------------------------------------------------


class TestCommitTypeVerification:
    """After a merge verdict, the landed squash commit type is checked."""

    async def _act(
        self,
        ctx,
        *,
        verdict="merge",
        item_commit_type="fix",
        reviewer_type="fix",
        workspace="default",
        subjects=None,
        failed=None,
        raises=None,
    ):
        from agent_gtd_dispatch.main import ReviewVerdict, _act_on_verdict

        if raises is not None:
            ctx.dispatch.find_landed_commit_subjects = MagicMock(side_effect=raises)
        else:
            ctx.dispatch.find_landed_commit_subjects = MagicMock(
                return_value=(
                    subjects if subjects is not None else {},
                    failed if failed is not None else [],
                )
            )
        ctx.gtd.complete_in_rollout = AsyncMock(return_value={"newly_ready": []})
        return await _act_on_verdict(
            "rollout-abc",
            "item-a",
            ReviewVerdict(
                verdict,
                "because",
                "note",
                "",
                reviewer_type,
                "groomed on the item",
                item_commit_type,
            ),
            token=None,
            attribution=None,
            workspace=_fake_workspace() if workspace == "default" else workspace,
        )

    @staticmethod
    def _check_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
        return [
            r
            for r in caplog.records
            if "rollout-loop: commit-type-check" in r.getMessage()
        ]

    @staticmethod
    def _bodies(ctx) -> list[str]:
        return [c.args[1] for c in ctx.gtd.post_comment.await_args_list]

    async def test_1_match(self, caplog: pytest.LogCaptureFixture) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE

        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
            patch("agent_gtd_dispatch.main._halt_rollout_from_loop") as halt,
        ):
            result = await self._act(ctx, subjects={"repo-a": "fix(item-a): Do it"})
        assert result == _TICK_CONTINUE
        assert ctx.gtd.post_comment.await_count == 1
        lines = self._check_lines(caplog)
        assert len(lines) == 1
        assert lines[0].levelno == logging.INFO
        assert "outcome=match" in lines[0].getMessage()
        assert "reviewer_agrees=True" in lines[0].getMessage()
        halt.assert_not_called()

    async def test_2_mismatch(self, caplog: pytest.LogCaptureFixture) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE

        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
            patch("agent_gtd_dispatch.main._halt_rollout_from_loop") as halt,
        ):
            result = await self._act(ctx, subjects={"repo-a": "feat(x): y"})
        assert result == _TICK_CONTINUE
        assert ctx.gtd.post_comment.await_count == 2
        mismatch = [b for b in self._bodies(ctx) if "commit type mismatch" in b]
        assert len(mismatch) == 1
        assert mismatch[0] == (
            "Rollout `rollout-abc`: commit type mismatch — item is groomed `fix` "
            "but landed squash commit(s) are `repo-a`: `feat(x): y`. Reviewer "
            "reported `fix`. Outcome unchanged."
        )
        lines = self._check_lines(caplog)
        assert len(lines) == 1
        assert lines[0].levelno == logging.WARNING
        assert "outcome=mismatch" in lines[0].getMessage()
        halt.assert_not_called()
        ctx.gtd.halt_rollout.assert_not_awaited()

    async def test_3_not_found(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            await self._act(ctx, subjects={}, failed=[])
        assert ctx.gtd.post_comment.await_count == 2
        found = [b for b in self._bodies(ctx) if "could not find a landed commit" in b]
        assert found == [
            "Rollout `rollout-abc`: could not find a landed commit with trailer "
            "`Item: item-a` to verify commit type `fix`. Outcome unchanged."
        ]
        lines = self._check_lines(caplog)
        assert len(lines) == 1
        assert lines[0].levelno == logging.WARNING
        assert "outcome=not_found" in lines[0].getMessage()

    async def test_4_unset_skips_lookup(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            await self._act(ctx, item_commit_type="")
        ctx.dispatch.find_landed_commit_subjects.assert_not_called()
        assert ctx.gtd.post_comment.await_count == 1
        lines = self._check_lines(caplog)
        assert len(lines) == 1
        assert lines[0].levelno == logging.INFO
        assert "outcome=skipped_unset" in lines[0].getMessage()

    async def test_5_lookup_raises(self, caplog: pytest.LogCaptureFixture) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE

        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
            patch("agent_gtd_dispatch.main._halt_rollout_from_loop") as halt,
        ):
            result = await self._act(ctx, raises=RuntimeError("git exploded"))
        assert result == _TICK_CONTINUE
        halt.assert_not_called()
        ctx.gtd.halt_rollout.assert_not_awaited()
        lines = self._check_lines(caplog)
        assert len(lines) == 1
        assert lines[0].levelno == logging.WARNING
        assert "outcome=error" in lines[0].getMessage()

    async def test_5b_post_comment_raises_is_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE

        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            ctx.gtd.post_comment = AsyncMock(
                side_effect=[None, RuntimeError("gtd down")]
            )
            result = await self._act(ctx, subjects={"repo-a": "feat(x): y"})
        assert result == _TICK_CONTINUE
        lines = self._check_lines(caplog)
        assert len(lines) == 1
        assert "outcome=error" in lines[0].getMessage()

    async def test_5c_verdict_comment_failure_does_not_suppress_check(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            ctx.gtd.post_comment = AsyncMock(side_effect=[RuntimeError("x"), None])
            await self._act(ctx, subjects={"repo-a": "feat(x): y"})
        assert ctx.gtd.post_comment.await_count == 2
        assert "commit type mismatch" in ctx.gtd.post_comment.await_args.args[1]
        assert "outcome=mismatch" in self._check_lines(caplog)[0].getMessage()

    async def test_6_skip_verdict_runs_no_lookup(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            await self._act(ctx, verdict="skip")
        ctx.dispatch.find_landed_commit_subjects.assert_not_called()
        assert self._check_lines(caplog) == []

    async def test_7_two_repos_only_the_feat_repo_is_named(self) -> None:
        with _loop_patch_stack() as ctx:
            await self._act(
                ctx,
                subjects={"repo-fix": "fix(a): b", "repo-feat": "feat(a): b"},
            )
        mismatch = [b for b in self._bodies(ctx) if "commit type mismatch" in b]
        assert len(mismatch) == 1
        assert "`repo-feat`: `feat(a): b`" in mismatch[0]
        assert "repo-fix" not in mismatch[0]

    async def test_8_unparsed_subject_is_a_mismatch(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            await self._act(ctx, subjects={"repo-a": "Merge branch x"})
        mismatch = [b for b in self._bodies(ctx) if "commit type mismatch" in b]
        assert len(mismatch) == 1
        assert "`repo-a`: `Merge branch x`" in mismatch[0]
        assert "outcome=mismatch" in self._check_lines(caplog)[0].getMessage()

    async def test_9_no_workspace(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            await self._act(ctx, workspace=None)
        ctx.dispatch.find_landed_commit_subjects.assert_not_called()
        lines = self._check_lines(caplog)
        assert len(lines) == 1
        assert lines[0].levelno == logging.WARNING
        assert "outcome=skipped_no_workspace" in lines[0].getMessage()

    async def test_10_all_repos_failed_is_error_without_comment(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            await self._act(ctx, subjects={}, failed=["repo-a"])
        assert ctx.gtd.post_comment.await_count == 1
        assert not any("could not find a landed commit" in b for b in self._bodies(ctx))
        lines = self._check_lines(caplog)
        assert len(lines) == 1
        assert lines[0].levelno == logging.WARNING
        assert "outcome=error" in lines[0].getMessage()

    async def test_11_match_but_reviewer_disagrees(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            await self._act(
                ctx, reviewer_type="feat", subjects={"repo-a": "fix(item-a): Do it"}
            )
        assert ctx.gtd.post_comment.await_count == 1
        lines = self._check_lines(caplog)
        assert len(lines) == 1
        assert lines[0].levelno == logging.WARNING
        assert "outcome=match" in lines[0].getMessage()
        assert "reviewer_agrees=False" in lines[0].getMessage()

    async def test_wave_tick_passes_the_workspace(self) -> None:
        from agent_gtd_dispatch.main import ReviewVerdict

        ws = _fake_workspace()
        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._launch_reviewer",
                new=AsyncMock(return_value=ReviewVerdict("skip", "r", "", "")),
            ),
            patch("agent_gtd_dispatch.main._act_on_verdict", new=AsyncMock()) as act,
        ):
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={
                    "next_ready": [],
                    "in_progress": ["item-a"],
                    "blocked": [],
                    "graph_complete": False,
                }
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            await _tick(ctx, workspace_ref=[ws])
        assert act.await_args.kwargs["workspace"] is ws


class TestWaveTickLandStep:
    """A merge verdict goes through the worker's land step before bookkeeping."""

    @staticmethod
    def _reviewable(ctx) -> None:
        ctx.gtd.advance_rollout = AsyncMock(
            return_value={
                "next_ready": [],
                "in_progress": ["item-a"],
                "blocked": [],
                "graph_complete": False,
            }
        )
        ctx.gtd.get_rollout = AsyncMock(
            return_value={"status": "running", "inFlightBuildRuns": []}
        )

    async def test_merge_lands_then_acts_on_the_returned_verdict(self) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE, ReviewVerdict

        original = ReviewVerdict("merge", "r", "note", "")
        landed = original._replace(rationale="r (land fix: y)")
        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._launch_reviewer",
                new=AsyncMock(return_value=original),
            ),
            patch(
                "agent_gtd_dispatch.main._land_reviewed_merge",
                new=AsyncMock(return_value=landed),
            ) as land,
            patch(
                "agent_gtd_dispatch.main._act_on_verdict",
                new=AsyncMock(return_value=_TICK_CONTINUE),
            ) as act,
        ):
            ctx.dispatch.land_origin_shas = MagicMock(return_value={"repo-a": "abc"})
            self._reviewable(ctx)
            assert await _tick(ctx) == _TICK_CONTINUE
        kwargs = land.await_args.kwargs
        assert kwargs["pre_review_origin"] == {"repo-a": "abc"}
        assert kwargs["loop_run_id"] == "loop-run-1"
        assert land.await_args.args[2] is original
        assert act.await_args.args[2] is landed

    async def test_land_halt_skips_act_on_verdict(self) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED, ReviewVerdict

        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._launch_reviewer",
                new=AsyncMock(return_value=ReviewVerdict("merge", "r", "n", "")),
            ),
            patch(
                "agent_gtd_dispatch.main._land_reviewed_merge",
                new=AsyncMock(return_value=_TICK_HALTED),
            ),
            patch("agent_gtd_dispatch.main._act_on_verdict", new=AsyncMock()) as act,
        ):
            self._reviewable(ctx)
            assert await _tick(ctx) == _TICK_HALTED
        act.assert_not_awaited()
        ctx.gtd.complete_in_rollout.assert_not_awaited()

    @pytest.mark.parametrize("verdict", ["re-dispatch", "skip", "halt"])
    async def test_other_verdicts_bypass_the_land(self, verdict) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE, ReviewVerdict

        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._launch_reviewer",
                new=AsyncMock(return_value=ReviewVerdict(verdict, "r", "", "")),
            ),
            patch("agent_gtd_dispatch.main._land_reviewed_merge") as land,
            patch(
                "agent_gtd_dispatch.main._act_on_verdict",
                new=AsyncMock(return_value=_TICK_CONTINUE),
            ),
        ):
            self._reviewable(ctx)
            await _tick(ctx)
        land.assert_not_called()

    async def test_drive_rollout_passes_its_run_id(self) -> None:
        from agent_gtd_dispatch.main import _TICK_DONE, _drive_rollout

        run = Run(project_name="p", mode="manage", rollout_id="rollout-abc", engine="x")
        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._rollout_wave_tick",
                new=AsyncMock(return_value=_TICK_DONE),
            ) as tick,
        ):
            ctx.gtd.get_rollout = AsyncMock(return_value={"project_id": "p1"})
            ctx.gtd.get_project = AsyncMock(return_value={"name": "P"})
            await _drive_rollout(run)
        assert tick.await_args.kwargs["loop_run_id"] == run.id


# ---------------------------------------------------------------------------
# Bookkeeping after a merge: retry, confirm on origin, never halt unrelated work
# ---------------------------------------------------------------------------


async def _act_bk(
    ctx,
    verdict="merge",
    *,
    rollout_id="r1",
    workspace="default",
    item_commit_type="",
    commit_type="",
    merge_note="note",
):
    from agent_gtd_dispatch.main import ReviewVerdict, _act_on_verdict

    return await _act_on_verdict(
        rollout_id,
        "item-a",
        ReviewVerdict(
            verdict,
            "because",
            merge_note,
            "",
            commit_type,
            "groomed" if commit_type else "",
            item_commit_type,
        ),
        token=None,
        attribution=None,
        workspace=_fake_workspace() if workspace == "default" else workspace,
    )


def _bodies(ctx) -> list[str]:
    return [c.args[1] for c in ctx.gtd.post_comment.await_args_list]


def _sleeps(ctx) -> list[float]:
    return [c.args[0] for c in ctx.sleep.await_args_list]


class TestBookkeepingRetry:
    async def test_transient_failure_then_success_does_not_halt(self) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE

        with _loop_patch_stack() as ctx:
            ctx.gtd.complete_in_rollout = AsyncMock(
                side_effect=[_http_error(503), {"newly_ready": ["item-b"]}]
            )
            ctx.dispatch.find_landed_commit_subjects = MagicMock()
            assert await _act_bk(ctx) == _TICK_CONTINUE

        assert ctx.gtd.complete_in_rollout.await_count == 2
        call = ctx.gtd.complete_in_rollout.await_args
        assert call.kwargs["merge_actor"] == "reviewer-autonomous"
        assert call.kwargs["decision_rule"] == "agent-judgment"
        ctx.gtd.halt_rollout.assert_not_awaited()
        assert _sleeps(ctx) == [2.0]
        bodies = _bodies(ctx)
        assert len(bodies) == 1
        assert "Downstream items unblocked: item-b." in bodies[0]
        assert "r1" not in ctx.unrecorded
        ctx.dispatch.find_landed_commit_subjects.assert_not_called()

    async def test_unconfirmed_merge_halts(self) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED

        with _loop_patch_stack() as ctx:
            ctx.gtd.complete_in_rollout = AsyncMock(side_effect=_http_error(503))
            ctx.dispatch.find_landed_commit_subjects = MagicMock(return_value=({}, []))
            assert await _act_bk(ctx) == _TICK_HALTED

        ctx.gtd.halt_rollout.assert_awaited_once()
        assert ctx.gtd.complete_in_rollout.await_count == 3
        assert _sleeps(ctx) == [2.0, 5.0]
        reason = ctx.gtd.halt_rollout.await_args.kwargs["reason"]
        assert reason.startswith("failed to record item item-a as completed:")
        assert "confirmation: no-trailer-found" in reason
        assert not any("item recorded as" in b for b in _bodies(ctx))
        assert ctx.dispatch.find_landed_commit_subjects.call_count == 1
        assert ctx.unrecorded == {}

    async def test_no_workspace_halts_without_lookup(self) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED

        with _loop_patch_stack() as ctx:
            ctx.gtd.complete_in_rollout = AsyncMock(side_effect=_http_error(503))
            ctx.dispatch.find_landed_commit_subjects = MagicMock(return_value=({}, []))
            assert await _act_bk(ctx, workspace=None) == _TICK_HALTED

        ctx.dispatch.find_landed_commit_subjects.assert_not_called()
        reason = ctx.gtd.halt_rollout.await_args.kwargs["reason"]
        assert "confirmation: no-workspace" in reason
        assert ctx.unrecorded == {}

    async def test_lookup_raised_halts(self, caplog: pytest.LogCaptureFixture) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED

        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.main"),
        ):
            ctx.gtd.complete_in_rollout = AsyncMock(side_effect=_http_error(422))
            ctx.dispatch.find_landed_commit_subjects = MagicMock(
                side_effect=RuntimeError("git")
            )
            assert await _act_bk(ctx) == _TICK_HALTED

        reason = ctx.gtd.halt_rollout.await_args.kwargs["reason"]
        assert "confirmation: lookup-raised" in reason
        assert any(
            "outcome=lookup-raised" in r.getMessage()
            and "exc=RuntimeError" in r.getMessage()
            for r in caplog.records
        )
        assert ctx.unrecorded == {}

    async def test_skip_persistent_failure_halts_without_lookup(self) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED

        with _loop_patch_stack() as ctx:
            ctx.gtd.complete_in_rollout = AsyncMock(side_effect=_http_error(503))
            ctx.dispatch.find_landed_commit_subjects = MagicMock(return_value=({}, []))
            assert await _act_bk(ctx, "skip") == _TICK_HALTED

        assert ctx.gtd.complete_in_rollout.await_count == 3
        assert _sleeps(ctx) == [2.0, 5.0]
        ctx.dispatch.find_landed_commit_subjects.assert_not_called()
        reason = ctx.gtd.halt_rollout.await_args.kwargs["reason"]
        assert reason.startswith("failed to record item item-a as skipped:")
        assert not any("item recorded as" in b for b in _bodies(ctx))

    async def test_confirmed_merge_warns_and_continues(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.main import _TICK_CONTINUE, _UnrecordedMerge

        with (
            _loop_patch_stack() as ctx,
            patch("agent_gtd_dispatch.main._monotonic", return_value=100.0),
            caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.main"),
            patch(
                "agent_gtd_dispatch.main._halt_rollout_from_loop", new=AsyncMock()
            ) as halt,
        ):
            ctx.gtd.complete_in_rollout = AsyncMock(side_effect=_http_error(503))
            ctx.dispatch.find_landed_commit_subjects = MagicMock(
                return_value=({"repo-a": "fix(x): y"}, [])
            )
            assert await _act_bk(ctx) == _TICK_CONTINUE

        halt.assert_not_called()
        ctx.gtd.halt_rollout.assert_not_awaited()
        assert ctx.unrecorded["r1"]["item-a"] == _UnrecordedMerge(
            1, "note", 100.0 + config.ROLLOUT_LOOP_POLL_SECONDS
        )
        assert ctx.gtd.post_comment.await_count == 1
        body = _bodies(ctx)[0]
        assert body.startswith(
            "Rollout `r1`: WARNING — reviewer merged this item (landed in: repo-a)"
        )
        assert "after 3 attempts" in body
        assert "HTTPStatusError" in body
        assert "Commit type: not reported by reviewer." in body
        assert ctx.dispatch.find_landed_commit_subjects.call_count == 1
        messages = [r.getMessage() for r in caplog.records]
        assert any("outcome=confirmed" in m for m in messages)
        assert any("bookkeeping-failed-after-merge" in m for m in messages)
        steps = [
            c.kwargs.get("current_step")
            for c in ctx.gtd.update_rollout_state.await_args_list
        ]
        assert "WARNING: item item-a merged but not recorded on the board" in steps

    async def test_confirmed_merge_still_checks_commit_type(self) -> None:
        with _loop_patch_stack() as ctx:
            ctx.gtd.complete_in_rollout = AsyncMock(side_effect=_http_error(503))
            ctx.dispatch.find_landed_commit_subjects = MagicMock(
                return_value=({"repo-a": "fix(x): y"}, [])
            )
            await _act_bk(ctx, item_commit_type="fix", commit_type="fix")

        assert ctx.dispatch.find_landed_commit_subjects.call_count == 2

    async def test_4xx_fails_fast_confirmed(self) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE

        with _loop_patch_stack() as ctx:
            ctx.gtd.complete_in_rollout = AsyncMock(side_effect=_http_error(422))
            ctx.dispatch.find_landed_commit_subjects = MagicMock(
                return_value=({"repo-a": "fix(x): y"}, [])
            )
            assert await _act_bk(ctx) == _TICK_CONTINUE

        assert ctx.gtd.complete_in_rollout.await_count == 1
        ctx.sleep.assert_not_awaited()
        assert "item-a" in ctx.unrecorded["r1"]
        assert _bodies(ctx)[0].startswith("Rollout `r1`: WARNING")

    async def test_4xx_fails_fast_unconfirmed_names_failed_repos(self) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED

        with _loop_patch_stack() as ctx:
            ctx.gtd.complete_in_rollout = AsyncMock(side_effect=_http_error(422))
            ctx.dispatch.find_landed_commit_subjects = MagicMock(
                return_value=({}, ["repo-a"])
            )
            assert await _act_bk(ctx) == _TICK_HALTED

        assert ctx.gtd.complete_in_rollout.await_count == 1
        ctx.sleep.assert_not_awaited()
        reason = ctx.gtd.halt_rollout.await_args.kwargs["reason"]
        assert "confirmation: no-trailer-found" in reason
        assert "failed_repos=repo-a" in reason


def _advance(completed, *, next_ready=None):
    return AsyncMock(
        return_value={
            "next_ready": next_ready or [],
            "in_progress": list(completed),
            "blocked": [],
            "graph_complete": False,
        }
    )


class TestUnrecordedMergeRetryPass:
    async def test_due_entry_recorded_without_rereview(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _TICK_CONTINUE, _UnrecordedMerge

        with (
            _loop_patch_stack() as ctx,
            patch("agent_gtd_dispatch.main._monotonic", return_value=100.0),
            patch("agent_gtd_dispatch.main._launch_reviewer") as reviewer,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            ctx.unrecorded["rollout-abc"] = {"item-a": _UnrecordedMerge(1, "note", 0.0)}
            ctx.gtd.advance_rollout = _advance(["item-a"])
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            ctx.gtd.complete_in_rollout = AsyncMock(
                return_value={"newly_ready": ["item-b"]}
            )
            assert await _tick(ctx) == _TICK_CONTINUE

        reviewer.assert_not_called()
        assert ctx.unrecorded["rollout-abc"] == {}
        kwargs = ctx.gtd.complete_in_rollout.await_args.kwargs
        assert kwargs["merge_note"] == "note"
        assert kwargs["outcome"] == "completed"
        assert any("outcome=recorded" in r.getMessage() for r in caplog.records)
        assert _bodies(ctx) == [
            "Rollout `rollout-abc`: board record for the merged item succeeded on "
            "retry pass 2. Downstream items unblocked: item-b."
        ]

    async def test_failed_retry_reviews_other_item(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import ReviewVerdict, _UnrecordedMerge

        with (
            _loop_patch_stack() as ctx,
            patch("agent_gtd_dispatch.main._monotonic", return_value=100.0),
            patch(
                "agent_gtd_dispatch.main._launch_reviewer",
                new=AsyncMock(return_value=ReviewVerdict("halt", "x", "", "")),
            ) as reviewer,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            ctx.unrecorded["rollout-abc"] = {"item-a": _UnrecordedMerge(1, "n", 0.0)}
            ctx.gtd.advance_rollout = _advance(["item-a", "item-b"])
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            ctx.gtd.complete_in_rollout = AsyncMock(side_effect=_http_error(503))
            await _tick(ctx)

        reviewer.assert_awaited_once()
        assert reviewer.await_args.args[1] == "item-b"
        assert ctx.unrecorded["rollout-abc"]["item-a"].passes == 2
        assert any("pass=2/10 outcome=failed" in r.getMessage() for r in caplog.records)

    async def test_not_due_is_skipped(self) -> None:
        from agent_gtd_dispatch.main import _TICK_WAIT, _UnrecordedMerge

        with (
            _loop_patch_stack() as ctx,
            patch("agent_gtd_dispatch.main._monotonic", return_value=100.0),
        ):
            ctx.unrecorded["rollout-abc"] = {"item-a": _UnrecordedMerge(1, "n", 200.0)}
            ctx.gtd.advance_rollout = _advance(["item-a"])
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            assert await _tick(ctx) == _TICK_WAIT

        ctx.gtd.complete_in_rollout.assert_not_awaited()
        assert ctx.unrecorded["rollout-abc"]["item-a"].passes == 1


class TestWaitOnUnrecordedMerges:
    async def test_waits_instead_of_stall_halt(self) -> None:
        from agent_gtd_dispatch.main import _TICK_WAIT, _UnrecordedMerge

        with (
            _loop_patch_stack() as ctx,
            patch("agent_gtd_dispatch.main._monotonic", return_value=100.0),
        ):
            ctx.unrecorded["rollout-abc"] = {"item-a": _UnrecordedMerge(1, "n", 0.0)}
            ctx.gtd.advance_rollout = _advance(["item-a"])
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            ctx.gtd.complete_in_rollout = AsyncMock(side_effect=_http_error(503))
            assert await _tick(ctx) == _TICK_WAIT

        assert ctx.unrecorded["rollout-abc"]["item-a"].passes == 2
        ctx.gtd.halt_rollout.assert_not_awaited()

    async def test_halts_at_pass_cap(self) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED, _UnrecordedMerge

        with (
            _loop_patch_stack() as ctx,
            patch("agent_gtd_dispatch.main._monotonic", return_value=100.0),
        ):
            ctx.unrecorded["rollout-abc"] = {"item-a": _UnrecordedMerge(9, "n", 0.0)}
            ctx.gtd.advance_rollout = _advance(["item-a"])
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            ctx.gtd.complete_in_rollout = AsyncMock(side_effect=_http_error(503))
            assert await _tick(ctx) == _TICK_HALTED

        reason = ctx.gtd.halt_rollout.await_args.kwargs["reason"]
        assert (
            "merged on origin but recording it on the board failed after 10 passes"
            in (reason)
        )
        assert "complete_item_in_rollout" in reason

    async def test_entry_left_in_progress_is_dropped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED, _UnrecordedMerge

        with (
            _loop_patch_stack() as ctx,
            patch("agent_gtd_dispatch.main._monotonic", return_value=100.0),
            caplog.at_level(logging.WARNING, logger="agent_gtd_dispatch.main"),
        ):
            ctx.unrecorded["rollout-abc"] = {"item-a": _UnrecordedMerge(1, "n", 0.0)}
            ctx.gtd.advance_rollout = _advance([])
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            assert await _tick(ctx) == _TICK_HALTED

        assert ctx.unrecorded["rollout-abc"] == {}
        assert any("unrecorded-merge-dropped" in r.getMessage() for r in caplog.records)
        assert "stalled" in ctx.gtd.halt_rollout.await_args.kwargs["reason"]
        ctx.gtd.complete_in_rollout.assert_not_awaited()


class TestDriveRolloutClearsUnrecordedMerges:
    async def test_finally_pops_entry(self) -> None:
        from agent_gtd_dispatch.main import (
            _TICK_DONE,
            _drive_rollout,
            _UnrecordedMerge,
        )

        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._rollout_wave_tick",
                new=AsyncMock(return_value=_TICK_DONE),
            ),
            patch("agent_gtd_dispatch.main.get_engine", return_value=MagicMock()),
        ):
            ctx.unrecorded["rollout-abc"] = {"item-a": _UnrecordedMerge(1, "n", 0.0)}
            ctx.gtd.get_rollout = AsyncMock(return_value={"project_id": "p"})
            ctx.gtd.get_project = AsyncMock(return_value={"name": "p"})
            assert await _drive_rollout(_make_run()) == _TICK_DONE

        assert "rollout-abc" not in ctx.unrecorded


# ---------------------------------------------------------------------------
# Transient board outages: the loop rides them out
# ---------------------------------------------------------------------------


def _read_timeout() -> httpx.ReadTimeout:
    return httpx.ReadTimeout("", request=httpx.Request("POST", "http://gtd.test/x"))


class TestIsTransientBoardError:
    @pytest.mark.parametrize(
        "exc",
        [
            _read_timeout(),
            httpx.ConnectTimeout("t"),
            httpx.ConnectError("refused"),
            httpx.RemoteProtocolError("eof"),
            _http_error(502),
            _http_error(503),
            _http_error(504),
        ],
    )
    def test_transient(self, exc) -> None:
        from agent_gtd_dispatch.main import _is_transient_board_error

        assert _is_transient_board_error(exc)

    @pytest.mark.parametrize(
        "exc",
        [_http_error(500), _http_error(404), _http_error(422), ValueError("x")],
    )
    def test_not_transient(self, exc) -> None:
        from agent_gtd_dispatch.main import _is_transient_board_error

        assert not _is_transient_board_error(exc)


class TestBoardOutageRetry:
    @staticmethod
    def _run() -> Run:
        return Run(
            project_name="p",
            mode="manage",
            rollout_id="rollout-abc",
            engine="claude-code",
        )

    @staticmethod
    def _board(ctx) -> None:
        ctx.gtd.get_rollout = AsyncMock(return_value={"project_id": "proj-1"})
        ctx.gtd.get_project = AsyncMock(
            return_value={"name": "P", "workspace_repos": []}
        )

    @pytest.mark.parametrize("exc_factory", [_read_timeout, lambda: _http_error(503)])
    async def test_transient_tick_errors_are_retried_and_the_run_succeeds(
        self, exc_factory, caplog
    ) -> None:
        from agent_gtd_dispatch import config as cfg
        from agent_gtd_dispatch.main import _TICK_DONE, _rollout_loop_worker

        caplog.set_level(logging.WARNING, logger="agent_gtd_dispatch.main")
        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._rollout_wave_tick",
                new=AsyncMock(side_effect=[exc_factory(), exc_factory(), _TICK_DONE]),
            ) as tick,
        ):
            self._board(ctx)
            await _rollout_loop_worker(self._run())

        assert tick.await_count == 3
        assert [c.args for c in ctx.sleep.await_args_list] == [
            (cfg.ROLLOUT_LOOP_POLL_SECONDS,),
            (cfg.ROLLOUT_LOOP_POLL_SECONDS,),
        ]
        kw = ctx.db.update_run.await_args.kwargs
        assert kw["status"] == RunStatus.succeeded
        assert kw["error"] == ""
        warnings = [
            r.getMessage() for r in caplog.records if "board-unavailable" in r.message
        ]
        assert len(warnings) == 2
        exc_name = type(exc_factory()).__name__
        assert "rollout_id=rollout-abc" in warnings[1]
        assert f"exc={exc_name}" in warnings[1]
        assert "consecutive=2" in warnings[1]

    @pytest.mark.parametrize("exc", [_http_error(500), ValueError("nope")])
    async def test_other_errors_propagate_on_first_occurrence(self, exc) -> None:
        from agent_gtd_dispatch.main import _TICK_DONE, _drive_rollout

        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._rollout_wave_tick",
                new=AsyncMock(side_effect=[exc, _TICK_DONE]),
            ) as tick,
        ):
            self._board(ctx)
            with pytest.raises(type(exc)):
                await _drive_rollout(self._run())

        assert tick.await_count == 1
        ctx.sleep.assert_not_awaited()

    async def test_non_transient_error_fails_the_loop_run_as_before(self) -> None:
        from agent_gtd_dispatch.main import _rollout_loop_worker

        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._rollout_wave_tick",
                new=AsyncMock(side_effect=_http_error(500)),
            ),
        ):
            self._board(ctx)
            await _rollout_loop_worker(self._run())

        kw = ctx.db.update_run.await_args.kwargs
        assert kw["status"] == RunStatus.failed
        assert kw["error"].startswith("rollout_loop_error: HTTPStatusError")

    async def test_initial_fetches_ride_out_the_outage(self) -> None:
        from agent_gtd_dispatch.main import _TICK_DONE, _drive_rollout

        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._rollout_wave_tick",
                new=AsyncMock(return_value=_TICK_DONE),
            ),
        ):
            ctx.gtd.get_rollout = AsyncMock(
                side_effect=[_read_timeout(), {"project_id": "proj-1"}]
            )
            ctx.gtd.get_project = AsyncMock(
                side_effect=[httpx.ConnectError("refused"), {"name": "P"}]
            )
            assert await _drive_rollout(self._run()) == _TICK_DONE

        assert ctx.gtd.get_rollout.await_count == 2
        assert ctx.gtd.get_project.await_count == 2
        assert ctx.sleep.await_count == 2

    async def test_a_persistent_outage_is_re_raised_once_the_window_elapses(
        self, monkeypatch
    ) -> None:
        import itertools

        from agent_gtd_dispatch import config as cfg
        from agent_gtd_dispatch.main import _rollout_loop_worker

        monkeypatch.setattr(cfg, "ROLLOUT_LOOP_BOARD_OUTAGE_SECONDS", 100)
        clock = itertools.count(0, 60)
        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._monotonic", side_effect=lambda: next(clock)
            ),
            patch(
                "agent_gtd_dispatch.main._rollout_wave_tick",
                new=AsyncMock(side_effect=lambda *a, **k: _raise(_read_timeout())),
            ) as tick,
        ):
            self._board(ctx)
            await _rollout_loop_worker(self._run())

        # t=0 starts the streak, t=60 is inside the window, t=120 exceeds it.
        assert tick.await_count == 3
        assert ctx.sleep.await_count == 2
        kw = ctx.db.update_run.await_args.kwargs
        assert kw["status"] == RunStatus.failed
        assert kw["error"].startswith("rollout_loop_error: ReadTimeout")

    async def test_a_clean_tick_resets_the_streak(self, monkeypatch) -> None:
        import itertools

        from agent_gtd_dispatch import config as cfg
        from agent_gtd_dispatch.main import _TICK_DONE, _TICK_WAIT, _drive_rollout

        monkeypatch.setattr(cfg, "ROLLOUT_LOOP_BOARD_OUTAGE_SECONDS", 100)
        clock = itertools.count(0, 60)
        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._monotonic", side_effect=lambda: next(clock)
            ),
            patch(
                "agent_gtd_dispatch.main._rollout_wave_tick",
                new=AsyncMock(
                    side_effect=[
                        _read_timeout(),  # t=0, streak starts
                        _read_timeout(),  # t=60
                        _TICK_WAIT,  # clean: streak resets
                        _read_timeout(),  # t=120, a NEW streak starts
                        _read_timeout(),  # t=180: 60s into it, not 180
                        _TICK_DONE,
                    ]
                ),
            ) as tick,
        ):
            self._board(ctx)
            assert await _drive_rollout(self._run()) == _TICK_DONE

        assert tick.await_count == 6


def _raise(exc: BaseException):
    raise exc


class TestTickIsSafeToReRunAfterATimeout:
    """AC4 guards: a timeout after a side effect, then a re-run, never repeats it."""

    async def test_transient_dispatch_error_escapes_instead_of_halting(self) -> None:
        from agent_gtd_dispatch.main import _TICK_WAIT

        attempts: dict[str, int] = {}
        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(
                side_effect=[
                    {
                        "next_ready": ["item-a"],
                        "in_progress": [],
                        "graph_complete": False,
                    },
                    # The timed-out dispatch DID land: the item is now dispatched.
                    {
                        "next_ready": [],
                        "in_progress": ["item-a"],
                        "graph_complete": False,
                    },
                ]
            )
            ctx.gtd.get_rollout = AsyncMock(
                side_effect=[
                    {"status": "running", "inFlightBuildRuns": []},
                    {
                        "status": "running",
                        "inFlightBuildRuns": [{"itemId": "item-a", "runId": "r1"}],
                    },
                ]
            )
            ctx.gtd.dispatch_item = AsyncMock(side_effect=_read_timeout())
            with pytest.raises(httpx.ReadTimeout):
                await _tick(ctx, dispatch_attempts=attempts)
            ctx.gtd.halt_rollout.assert_not_awaited()

            assert await _tick(ctx, dispatch_attempts=attempts) == _TICK_WAIT

        ctx.gtd.dispatch_item.assert_awaited_once()
        ctx.gtd.halt_rollout.assert_not_awaited()

    async def test_non_transient_dispatch_error_still_halts(self) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED

        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={
                    "next_ready": ["item-a"],
                    "in_progress": [],
                    "graph_complete": False,
                }
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            ctx.gtd.dispatch_item = AsyncMock(side_effect=_http_error(500))
            assert await _tick(ctx) == _TICK_HALTED

        assert (
            "dispatch of item item-a failed"
            in (ctx.gtd.halt_rollout.await_args.kwargs["reason"])
        )

    async def test_review_item_is_fetched_before_the_reviewer_and_handed_on(
        self,
    ) -> None:
        from agent_gtd_dispatch.main import ReviewVerdict

        item = {"id": "item-a", "title": "t"}
        verdict = ReviewVerdict("merge", "r", "note", "")
        order: list[str] = []
        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._launch_reviewer",
                new=AsyncMock(
                    side_effect=lambda *a, **k: order.append("reviewer") or verdict
                ),
            ) as reviewer,
            patch(
                "agent_gtd_dispatch.main._land_reviewed_merge",
                new=AsyncMock(return_value=verdict),
            ) as land,
            patch(
                "agent_gtd_dispatch.main._verify_landed_commit_type", new=AsyncMock()
            ),
        ):
            ctx.gtd.get_item = AsyncMock(
                side_effect=lambda *a, **k: order.append("get_item") or item
            )
            ctx.dispatch.land_origin_shas = MagicMock(return_value={})
            ctx.gtd.advance_rollout = AsyncMock(
                return_value={
                    "next_ready": [],
                    "in_progress": ["item-a"],
                    "graph_complete": False,
                }
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"status": "running", "inFlightBuildRuns": []}
            )
            await _tick(ctx)

        assert order == ["get_item", "reviewer"]
        assert reviewer.await_args.kwargs["item"] is item
        assert land.await_args.kwargs["item"] is item

    async def test_launch_reviewer_with_an_item_makes_no_board_item_read(
        self,
    ) -> None:
        item = TestLaunchReviewer._item()
        with _loop_patch_stack() as ctx:
            await TestLaunchReviewer()._launch(
                ctx,
                verdict_blob={"verdict": "merge", "rationale": "ok"},
                reason="ok",
                prefetched=item,
            )
            ctx.gtd.get_item.assert_not_awaited()
        assert ctx.dispatch.build_review_prompt.call_args.args[0] is item


# ---------------------------------------------------------------------------
# The resident-manager keep-alive machinery is retired (item 7baf77d0)
# ---------------------------------------------------------------------------


class TestStaleUnownedRolloutIsNeverRelaunched:
    """Regression for rollout fe1e828d (2026-10-21).

    One host's watchdog saw a rollout whose `manager_state_updated_at`
    was 2137s old (the reviewing phase publishes no heartbeat), had nothing for
    it in its in-memory map because it never owned it, and started a SECOND
    loop for a rollout another host's loop was still driving. Under the
    worker-driven loop nothing may do that: the watchdog is gone, and the
    re-adoption sweep resumes only rollouts this host holds a MANAGE row for.
    """

    @staticmethod
    def _stale_unowned_rollout() -> dict:
        ancient = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        return {
            "id": "rollout-on-another-host",
            "project_name": "p",
            "project_id": "proj-1",
            "status": "running",
            "manager_phase": "reviewing",
            "manager_state_updated_at": ancient,
            "manage_retry_count": 0,
            "inFlightBuildRuns": [],
        }

    async def test_nothing_starts_a_loop_or_relaunches_it(self) -> None:
        from agent_gtd_dispatch import main

        rollout = self._stale_unowned_rollout()
        with (
            _loop_patch_stack() as ctx,
            patch("agent_gtd_dispatch.main._start_rollout_loop") as mock_loop,
        ):
            # This host only ever ran a CHILD BUILD of the rollout: no loop row.
            ctx.db.list_runs_by_rollout = AsyncMock(
                return_value=[
                    _make_run(mode="build", rollout_id="rollout-on-another-host")
                ]
            )
            ctx.gtd.list_running_rollouts = AsyncMock(return_value=[rollout])
            ctx.gtd.get_rollout = AsyncMock(return_value=rollout)
            ctx.gtd.relaunch_manage_rollout = AsyncMock(
                return_value={**rollout, "manage_retry_count": 1}
            )

            # Every periodic liveness pass the service runs, startup and steady
            # state. The watchdog tick is looked up by name so this test also
            # fails on a tree where it still exists.
            await main._readopt_running_rollouts()
            for name in ("_watchdog_tick",):
                tick = getattr(main, name, None)
                if tick is not None:
                    await tick()

        mock_loop.assert_not_called()
        ctx.db.insert_run.assert_not_called()
        ctx.gtd.relaunch_manage_rollout.assert_not_called()
        ctx.gtd.halt_rollout.assert_not_called()

    def test_the_keep_alive_machinery_is_gone(self) -> None:
        from agent_gtd_dispatch import config, gtd_client, main

        for name in (
            "_maybe_relaunch_manage",
            "_do_manage_recovery",
            "_manage_watchdog",
            "_watchdog_tick",
            "_watchdog_evaluate_rollout",
            "_watchdog_task",
            "_watchdog_acted",
            "_rollout_to_run",
            "_manage_free_relaunches",
            "_manage_last_terminal_count",
            "_manage_seen_in_flight_item_ids",
            "_CLEAN_EXIT_STATUSES",
            "MAX_MANAGE_RETRIES",
            "MANAGE_RETRY_BACKOFF_SECONDS",
        ):
            assert not hasattr(main, name), name
        for name in (
            "MAX_MANAGE_RETRIES",
            "MAX_MANAGE_FREE_RELAUNCHES",
            "MANAGE_FREE_RELAUNCH_MIN_UPTIME_SECONDS",
            "MANAGE_STALE_THRESHOLD_SECONDS",
            "WATCHDOG_INTERVAL_SECONDS",
            "MANAGE_RETRY_BACKOFF_SECONDS",
        ):
            assert not hasattr(config, name), name
        assert not hasattr(gtd_client, "relaunch_manage_rollout")
        # The survivors.
        assert hasattr(config, "MANAGE_TIMEOUT_SECONDS")
        assert hasattr(gtd_client, "list_running_rollouts")


class TestNeverHaltWhileABuildIsInFlight:
    """Invariant formerly encoded by the deferred cap-exceeded halt (39a77ede).

    Halting a rollout while a child build is still running strands that build:
    it finishes and pushes with nobody left to review or merge it. The ladder
    deferred its cap halt for exactly that reason. In the worker-driven loop
    the guarantee is structural — the loop's only liveness halt ("stalled")
    requires nothing in flight, and with builds in flight the loop waits.
    """

    @staticmethod
    def _advance(**kw) -> dict:
        return {
            "next_ready": [],
            "in_progress": ["item-a", "item-b"],
            "graph_complete": False,
            **kw,
        }

    @staticmethod
    def _running(in_flight: list[dict]) -> dict:
        return {"status": "running", "inFlightBuildRuns": in_flight}

    async def test_builds_in_flight_and_nothing_else_waits_instead_of_halting(
        self,
    ) -> None:
        from agent_gtd_dispatch.main import _TICK_WAIT

        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(return_value=self._advance())
            ctx.gtd.get_rollout = AsyncMock(
                return_value=self._running(
                    [
                        {"runId": "r-a", "itemId": "item-a", "status": "running"},
                        {"runId": "r-b", "itemId": "item-b", "status": "pending"},
                    ]
                )
            )
            assert await _tick(ctx) == _TICK_WAIT

        ctx.gtd.halt_rollout.assert_not_called()

    async def test_a_stale_heartbeat_never_halts_an_in_flight_wave(self) -> None:
        """manager_state_updated_at is not a liveness signal any more."""
        from agent_gtd_dispatch.main import _TICK_WAIT

        ancient = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(
                return_value=self._advance(in_progress=["item-a"])
            )
            ctx.gtd.get_rollout = AsyncMock(
                return_value={
                    **self._running(
                        [{"runId": "r-a", "itemId": "item-a", "status": "running"}]
                    ),
                    "manager_phase": "polling",
                    "manager_state_updated_at": ancient,
                    "manage_retry_count": 99,
                }
            )
            assert await _tick(ctx) == _TICK_WAIT

        ctx.gtd.halt_rollout.assert_not_called()

    async def test_stall_halt_fires_only_with_nothing_in_flight(self) -> None:
        from agent_gtd_dispatch.main import _TICK_HALTED

        with _loop_patch_stack() as ctx:
            ctx.gtd.advance_rollout = AsyncMock(
                return_value=self._advance(in_progress=[], blocked=["item-z"])
            )
            ctx.gtd.get_rollout = AsyncMock(return_value=self._running([]))
            assert await _tick(ctx) == _TICK_HALTED

        ctx.gtd.halt_rollout.assert_awaited_once()
        assert "rollout stalled" in ctx.gtd.halt_rollout.call_args.kwargs["reason"]

    async def test_a_crashed_loop_fails_its_run_but_never_halts_the_rollout(
        self,
    ) -> None:
        from agent_gtd_dispatch.main import _rollout_loop_worker

        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._drive_rollout",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            await _rollout_loop_worker(_make_run())

        ctx.gtd.halt_rollout.assert_not_called()
        statuses = [c.kwargs.get("status") for c in ctx.db.update_run.call_args_list]
        assert statuses[-1] == RunStatus.failed


class TestNeverWaitOnARunByBackgroundingIt:
    """Invariant formerly encoded by the manage prompt's Step 3 (3889efbb).

    Under ``claude --print`` a backgrounded wait means the turn ends and the
    process dies. The worker-driven loop awaits each poll interval and each
    reviewer in its own task's foreground, and every agent prompt it launches
    carries the shared turn-discipline block.
    """

    async def test_the_loop_awaits_its_poll_interval_inline(self) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.main import _TICK_DONE, _TICK_WAIT, _drive_rollout

        with (
            _loop_patch_stack() as ctx,
            patch(
                "agent_gtd_dispatch.main._rollout_wave_tick",
                new=AsyncMock(side_effect=[_TICK_WAIT, _TICK_WAIT, _TICK_DONE]),
            ),
            patch("agent_gtd_dispatch.main.get_engine", return_value=MagicMock()),
            patch("agent_gtd_dispatch.main.asyncio.create_task") as create_task,
        ):
            ctx.gtd.get_rollout = AsyncMock(return_value={"project_id": "p"})
            ctx.gtd.get_project = AsyncMock(return_value={"name": "p"})
            assert await _drive_rollout(_make_run()) == _TICK_DONE

        assert [c.args[0] for c in ctx.sleep.await_args_list] == [
            config.ROLLOUT_LOOP_POLL_SECONDS,
            config.ROLLOUT_LOOP_POLL_SECONDS,
        ]
        create_task.assert_not_called()

    def test_the_reviewer_prompt_carries_the_turn_discipline_block(self) -> None:
        from agent_gtd_dispatch import dispatch

        prompt = dispatch.build_review_prompt(
            {"id": "item-a", "title": "t", "acceptance_criteria": ["works"]},
            {"name": "p", "gate_command": "make check"},
            "rollout-abc",
            "feat/item-a-x",
            40,
            Path("/tmp/ws"),  # noqa: S108 — prompt text only
            repo_dirs=["repo-a"],
            default_branches={"repo-a": "main"},
            workspace_mode=False,
            run_status="succeeded",
            gate_result="passed",
            merge_notes=[],
            redispatches_used=0,
            redispatch_cap=1,
        )
        assert (
            "**Never end your turn while anything you started is still running.**"
            in prompt
        )
        assert "no `run_in_background: true`" in prompt


class TestSurvivingManageBehaviour:
    """What the retirement deliberately left in place."""

    async def test_manage_timeout_preserves_the_workspace_and_relaunches_nothing(
        self, tmp_path
    ) -> None:
        """should_cleanup = False on a manage timeout survives the retirement."""
        import subprocess

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import CLAUDE
        from agent_gtd_dispatch.main import _dispatch_worker

        await db.init_db()
        run = Run(
            item_id=None,
            project_name="test-project",
            mode="manage",
            rollout_id="rollout-abc",
            engine="claude-code",
        )
        await db.insert_run(run)

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
            patch("agent_gtd_dispatch.main._start_rollout_loop") as mock_loop,
        ):
            mock_gtd.get_rollout = AsyncMock(
                return_value={"id": "rollout-abc", "project_id": "p1"}
            )
            mock_gtd.get_project = AsyncMock(
                return_value={
                    "id": "p1",
                    "name": "proj",
                    "git_origin": "git@host:repos/repo",
                }
            )
            mock_gtd.get_rollout_merge_notes = AsyncMock(return_value=[])
            mock_dispatch.prepare_manage_workspace.return_value = tmp_path
            mock_dispatch.build_system_prompt.return_value = "prompt"
            mock_dispatch.run_agent = AsyncMock(
                side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=3600)
            )
            mock_dispatch.cleanup_workspace = MagicMock()

            await _dispatch_worker(run, 100, CLAUDE, 3600)

        mock_dispatch.cleanup_workspace.assert_not_called()
        mock_loop.assert_not_called()
        final = await db.get_run(run.id)
        assert final is not None
        assert final.status == RunStatus.timed_out

    async def test_retention_task_created_on_startup_and_cancelled_on_shutdown(
        self,
    ) -> None:
        from fastapi.testclient import TestClient

        from agent_gtd_dispatch import main

        main._retention_task = None

        with (
            patch("agent_gtd_dispatch.main._readopt_running_rollouts", new=AsyncMock()),
            TestClient(main.app),
        ):
            captured = main._retention_task
            assert captured is not None, "Retention task was not created on startup"
            assert not captured.done()

        assert captured.done(), "Retention task should be done after shutdown"

    def test_manage_timeout_is_sized_for_a_reviewer(self) -> None:
        from agent_gtd_dispatch import config

        assert config.MANAGE_TIMEOUT_SECONDS == 45 * 60
        assert config.MANAGE_TIMEOUT_SECONDS == config.REVIEW_TIMEOUT_SECONDS
        assert config.timeout_seconds_for_mode("manage") == 45 * 60


# ---------------------------------------------------------------------------
# One loop per rollout per host: the _start_rollout_loop choke point
# ---------------------------------------------------------------------------


class TestOneLoopPerRolloutGuard:
    async def test_choke_point_registers_synchronously_and_refuses_a_duplicate(
        self,
    ) -> None:
        from agent_gtd_dispatch import main

        never_finish = asyncio.Event()

        async def _fake_worker(run, *, attribution=None):
            await never_finish.wait()

        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_rollout_loop_worker", _fake_worker),
        ):
            first = _make_run()
            assert main._start_rollout_loop(first) is True
            # Registered before the task has had a chance to run at all.
            assert ctx.rollout_loops["rollout-abc"] is first
            assert first.id in ctx.active

            second = _make_run()
            assert main._start_rollout_loop(second) is False
            assert ctx.rollout_loops["rollout-abc"] is first
            assert second.id not in ctx.active

            never_finish.set()
            await asyncio.gather(*ctx.active.values())

    async def test_sweep_does_not_start_a_second_loop_a_dispatch_started_meanwhile(
        self,
    ) -> None:
        from agent_gtd_dispatch import main

        never_finish = asyncio.Event()

        async def _fake_worker(run, *, attribution=None):
            await never_finish.wait()

        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_rollout_loop_worker", _fake_worker),
        ):
            dispatched = _make_run()

            async def _insert_run(run):
                # A MANAGE /dispatch lands while the sweep awaits insert_run.
                assert main._start_rollout_loop(dispatched) is True

            ctx.db.insert_run = AsyncMock(side_effect=_insert_run)
            ctx.gtd.list_running_rollouts = AsyncMock(
                return_value=[{"id": "rollout-abc", "project_name": "p"}]
            )
            await main._readopt_running_rollouts()
            # Let the scheduled cancel write run (asyncio.sleep is mocked here).
            await asyncio.gather(*list(main._background_tasks))

            assert ctx.rollout_loops["rollout-abc"] is dispatched
            assert list(ctx.active) == [dispatched.id]
            swept = ctx.db.insert_run.call_args.args[0]
            cancel = [
                c for c in ctx.db.update_run.call_args_list if c.args[0] == swept.id
            ]
            assert cancel
            assert cancel[-1].kwargs["status"] == RunStatus.cancelled
            assert "already driven" in cancel[-1].kwargs["error"]

            never_finish.set()
            await asyncio.gather(*ctx.active.values())

    async def test_queued_refusal_cancels_the_local_row(self) -> None:
        from agent_gtd_dispatch import main
        from agent_gtd_dispatch.engines import get_engine

        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_pending_queue", []) as queue,
        ):
            ctx.rollout_loops["rollout-abc"] = _make_run()
            queued = _make_run()
            queue.append(
                main._PendingDispatch(
                    run=queued,
                    engine=get_engine("claude-code"),
                    max_turns=10,
                    timeout_seconds=60,
                    attribution=None,
                )
            )
            main._try_start_pending()
            await asyncio.gather(*list(main._background_tasks))

            assert queue == []
            assert queued.id not in ctx.active
            ctx.db.update_run.assert_awaited_once()
            assert ctx.db.update_run.call_args.args[0] == queued.id
            assert ctx.db.update_run.call_args.kwargs["status"] == RunStatus.cancelled

    async def test_halted_loop_deregisters_before_its_bookkeeping_awaits(
        self,
    ) -> None:
        from agent_gtd_dispatch import main

        real_worker = main._rollout_loop_worker
        seen: dict[str, object] = {}

        with (
            _loop_patch_stack() as ctx,
            patch.object(
                main, "_rollout_wave_tick", AsyncMock(return_value=main._TICK_HALTED)
            ),
            patch.object(main, "_publish_rollout_phase", AsyncMock()),
        ):
            ctx.gtd.get_rollout = AsyncMock(
                return_value={"id": "rollout-abc", "project_id": "proj"}
            )
            ctx.gtd.get_project = AsyncMock(return_value={"name": "p"})
            fresh = _make_run()

            async def _update_run(run_id, **kwargs):
                if kwargs.get("status") == RunStatus.failed:
                    # The terminal bookkeeping write: the halted loop must no
                    # longer hold the rollout, so a relaunch is accepted.
                    seen["live"] = main._live_rollout_driver("rollout-abc")
                    with patch.object(main, "_rollout_loop_worker", AsyncMock()):
                        seen["started"] = main._start_rollout_loop(fresh)

            ctx.db.update_run = AsyncMock(side_effect=_update_run)
            halted = _make_run()
            ctx.rollout_loops["rollout-abc"] = halted
            await real_worker(halted)

            assert seen == {"live": None, "started": True}
            # The halted loop's finally must not evict the fresh loop.
            assert ctx.rollout_loops["rollout-abc"] is fresh
            await asyncio.gather(*[t for t in ctx.active.values() if t])
