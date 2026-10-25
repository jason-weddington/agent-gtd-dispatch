"""Concurrent reviews in the rollout loop (item 95c346c4).

Reviews of completed builds run as background tasks, several at once, each in
its own slot workspace; ready items are dispatched on every tick; the land
step stays strictly serialized per rollout.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import signal
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agent_gtd_dispatch import config, dispatch, main
from agent_gtd_dispatch.main import (
    _TICK_CONTINUE,
    _TICK_DONE,
    _TICK_HALTED,
    _TICK_PARKED,
    _TICK_WAIT,
    ReviewVerdict,
    _ActiveLand,
    _QueuedLand,
    _ReviewEntry,
    _RolloutLoopState,
)
from agent_gtd_dispatch.models import Run, RunStatus
from tests.test_manage_recovery import _http_error, _loop_patch_stack

ROLLOUT = "rollout-abc"
LOGGER = "agent_gtd_dispatch.main"


@pytest.fixture(autouse=True)
def _env(tmp_path):
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
    }
    with patch.dict(os.environ, env):
        config.load()
        yield


@pytest.fixture(autouse=True)
def _fresh_host_counter(monkeypatch):
    monkeypatch.setattr(main, "_host_reviews_in_flight", 0)


async def _yield(n: int = 5) -> None:
    """Let ready tasks run (asyncio.sleep is patched by the loop stack)."""
    loop = asyncio.get_running_loop()
    for _ in range(n):
        fut = loop.create_future()
        loop.call_soon(fut.set_result, None)
        await fut


def _ws(tmp_path: Path, slot: int, *, repos: tuple[str, ...] = ("repo-a",)):
    root = tmp_path / f"slot-{slot}"
    root.mkdir(exist_ok=True)
    return SimpleNamespace(
        root=root,
        repo_paths={r: root / r for r in repos},
        default_branches=dict.fromkeys(repos, "main"),
        created=True,
        workspace_mode=True,
    )


def _wire(ctx, tmp_path: Path) -> None:
    """Default collaborators: per-slot workspaces, no landed commit, no runs."""
    ctx.dispatch.prepare_rollout_workspace = MagicMock(
        side_effect=lambda _rid, *, git_origin, repo_urls, slot: _ws(tmp_path, slot)
    )
    ctx.dispatch.find_landed_commit_subjects = MagicMock(return_value=({}, []))
    ctx.dispatch.land_origin_shas = MagicMock(return_value={"repo-a": "abc"})
    ctx.gtd.list_runs_for_item = AsyncMock(return_value=[])
    ctx.gtd.get_item = AsyncMock(
        side_effect=lambda item_id, **_kw: {"id": item_id, "title": "t"}
    )


def _board(
    ctx,
    *,
    in_progress=(),
    next_ready=(),
    blocked=(),
    in_flight=(),
    status="running",
    drain=None,
    graph_complete=False,
) -> None:
    ctx.gtd.advance_rollout = AsyncMock(
        return_value={
            "next_ready": list(next_ready),
            "in_progress": list(in_progress),
            "blocked": list(blocked),
            "graph_complete": graph_complete,
        }
    )
    ctx.gtd.get_rollout = AsyncMock(
        return_value={
            "status": status,
            "loop_host_drain_reason": drain,
            "inFlightBuildRuns": [
                {"runId": f"r-{i}", "itemId": i, "status": "running"} for i in in_flight
            ],
        }
    )


async def _tick(ctx, ls, *, rollout_id=ROLLOUT, **overrides):
    kwargs: dict[str, Any] = {
        "project": {"name": "p", "workspace_repos": ["git@h:o/repo-a"]},
        "loop_state": ls,
        "reviewer_engine": MagicMock(name="reviewer"),
        "dispatch_attempts": {},
        "token": None,
        "attribution": None,
        "loop_run_id": "loop-run-1",
    }
    kwargs.update(overrides)
    return await main._rollout_wave_tick(rollout_id, **kwargs)


class FakeReviewer:
    """A reviewer whose verdict for each item is released by the test."""

    def __init__(self) -> None:
        self.gates: dict[str, asyncio.Event] = {}
        self.verdicts: dict[str, ReviewVerdict] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.cancelled: list[str] = []
        self.called = asyncio.Event()

    def _gate(self, item_id: str) -> asyncio.Event:
        return self.gates.setdefault(item_id, asyncio.Event())

    async def __call__(self, rollout_id: str, item_id: str, **kw: Any):
        self.calls.append((item_id, kw))
        self.called.set()
        try:
            await self._gate(item_id).wait()
        except asyncio.CancelledError:
            self.cancelled.append(item_id)
            raise
        return self.verdicts.get(item_id, ReviewVerdict("skip", "r", "", ""))

    def release(self, item_id: str, verdict: ReviewVerdict | None = None) -> None:
        if verdict is not None:
            self.verdicts[item_id] = verdict
        self._gate(item_id).set()

    def items(self) -> list[str]:
        return [c[0] for c in self.calls]


def _pending_task() -> asyncio.Task[Any]:
    return asyncio.create_task(asyncio.Event().wait())


def _done_task(result: Any) -> asyncio.Task[Any]:
    async def _r():
        return result

    return asyncio.create_task(_r())


def _entry(item_id: str, slot: int, task: asyncio.Task[Any]) -> _ReviewEntry:
    return _ReviewEntry(item_id, slot, task, {"id": item_id}, {"repo-a": "abc"}, 0.0)


def _queued(item_id: str, slot: int) -> _QueuedLand:
    return _QueuedLand(
        item_id,
        slot,
        ReviewVerdict("merge", "r", "n", ""),
        {"id": item_id},
        {"repo-a": "abc"},
        0.0,
    )


async def _cleanup(ls: _RolloutLoopState) -> None:
    tasks = [e.task for e in ls.reviews.values()]
    if ls.active_land is not None:
        tasks.append(ls.active_land.task)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# AC1 — config and the host-wide limit
# ---------------------------------------------------------------------------


class TestConfig:
    @pytest.mark.parametrize(
        ("value", "expected"), [(None, 3), ("5", 5), ("0", 1), ("-2", 1)]
    )
    def test_max_concurrent_reviews(self, monkeypatch, value, expected) -> None:
        if value is None:
            monkeypatch.delenv("DISPATCH_ROLLOUT_MAX_CONCURRENT_REVIEWS", raising=False)
        else:
            monkeypatch.setenv("DISPATCH_ROLLOUT_MAX_CONCURRENT_REVIEWS", value)
        config.load()
        assert expected == config.ROLLOUT_MAX_CONCURRENT_REVIEWS


class TestHostWideLimit:
    async def test_two_loops_share_the_host_limit(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "ROLLOUT_MAX_CONCURRENT_REVIEWS", 2)
        reviewer = FakeReviewer()
        ls1, ls2 = _RolloutLoopState(), _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a1", "a2"])
            await _tick(ctx, ls1, rollout_id="r-one")
            _board(ctx, in_progress=["b1", "b2"])
            await _tick(ctx, ls2, rollout_id="r-two")
            await _yield()
            assert len(ls1.reviews) + len(ls2.reviews) == 2
            assert main._host_reviews_in_flight == 2
            assert len(reviewer.calls) == 2
            await _cleanup(ls1)
            await _cleanup(ls2)
        assert main._host_reviews_in_flight == 0

    async def test_limit_one_still_dispatches_ready_items(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "ROLLOUT_MAX_CONCURRENT_REVIEWS", 1)
        reviewer = FakeReviewer()
        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a", "b"], next_ready=["c"])
            assert await _tick(ctx, ls) == _TICK_CONTINUE
            await _yield()
            assert list(ls.reviews) == ["a"]
            ctx.gtd.dispatch_item.assert_awaited_once_with(
                "c", rollout_id=ROLLOUT, token=None
            )
            # The second item waits for the single host-wide review slot.
            _board(ctx, in_progress=["a", "b", "c"], in_flight=["c"])
            assert await _tick(ctx, ls) == _TICK_WAIT
            assert reviewer.items() == ["a"]
            reviewer.release("a")
            await _yield()
            await _tick(ctx, ls)
            await _yield()
            assert reviewer.items() == ["a", "b"]
            await _cleanup(ls)


# ---------------------------------------------------------------------------
# AC2 — dedicated reviewer thread pool
# ---------------------------------------------------------------------------


class TestReviewerPool:
    async def test_blocked_reviewers_do_not_starve_the_shared_pool(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 2)
        monkeypatch.setattr(config, "ROLLOUT_MAX_CONCURRENT_REVIEWS", 3)
        dispatch.init_executor()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        try:
            assert dispatch._review_executor is not None
            blocked = [
                loop.run_in_executor(dispatch._review_executor, release.wait)
                for _ in range(3)
            ]
            result = await asyncio.wait_for(
                loop.run_in_executor(dispatch._executor, lambda: 42), timeout=1.0
            )
            assert result == 42
        finally:
            release.set()
            await asyncio.gather(*blocked)
            dispatch.init_executor()

    async def test_loop_started_review_passes_the_review_pool(self, tmp_path) -> None:
        reviewer = FakeReviewer()
        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
        ):
            _wire(ctx, tmp_path)
            ctx.dispatch._review_executor = sentinel = object()
            _board(ctx, in_progress=["a"])
            await _tick(ctx, ls)
            await _yield()
            assert reviewer.calls[0][1]["executor"] is sentinel
            await _cleanup(ls)


# ---------------------------------------------------------------------------
# AC3 / AC4 / AC11 — starting reviews into isolated slots
# ---------------------------------------------------------------------------


class TestStartingReviews:
    async def test_two_reviewable_items_start_in_one_tick_in_distinct_slots(
        self, tmp_path, caplog
    ) -> None:
        reviewer = FakeReviewer()
        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a", "b"])
            assert await _tick(ctx, ls) == _TICK_CONTINUE
            await _yield()
            assert {i: e.slot for i, e in ls.reviews.items()} == {"a": 0, "b": 1}
            roots = {kw["workspace"].root for _, kw in reviewer.calls}
            assert roots == {tmp_path / "slot-0", tmp_path / "slot-1"}
            slots = [
                c.kwargs["slot"]
                for c in ctx.dispatch.prepare_rollout_workspace.call_args_list
            ]
            assert slots == [0, 1]
            await _cleanup(ls)
        starts = [
            r.getMessage() for r in caplog.records if "review-start" in r.getMessage()
        ]
        assert len(starts) == 2
        assert "slot=1" in starts[1]
        assert "host_reviews_in_flight=2/3" in starts[1]

    async def test_one_reviewable_item_only_clones_slot_zero(self, tmp_path) -> None:
        reviewer = FakeReviewer()
        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a"])
            await _tick(ctx, ls)
            reviewer.release("a")
            await _yield()
            _board(ctx, in_progress=["b"])
            await _tick(ctx, ls)
            await _yield()
            assert list(ls.slots) == [0]
            assert ctx.dispatch.prepare_rollout_workspace.call_count == 1
            await _cleanup(ls)

    async def test_pruned_slot_root_is_re_prepared(self, tmp_path) -> None:
        reviewer = FakeReviewer()
        gone = SimpleNamespace(
            root=tmp_path / "pruned",
            repo_paths={"repo-a": tmp_path / "pruned"},
            default_branches={"repo-a": "main"},
            created=False,
            workspace_mode=True,
        )
        ls = _RolloutLoopState(slots={0: gone})
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a"])
            await _tick(ctx, ls)
            await _yield()
            assert ls.slots[0] is not gone
            assert ls.slots[0].root == tmp_path / "slot-0"
            assert reviewer.calls[0][1]["workspace"] is ls.slots[0]
            await _cleanup(ls)

    async def test_third_item_starts_once_a_slot_frees(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "ROLLOUT_MAX_CONCURRENT_REVIEWS", 2)
        reviewer = FakeReviewer()
        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a", "b", "c"])
            await _tick(ctx, ls)
            await _yield()
            assert reviewer.items() == ["a", "b"]
            assert await _tick(ctx, ls) == _TICK_WAIT
            assert reviewer.items() == ["a", "b"]
            reviewer.release("a")
            await _yield()
            await _tick(ctx, ls)
            await _yield()
            assert reviewer.items() == ["a", "b", "c"]
            assert ls.reviews["c"].slot == 0
            await _cleanup(ls)

    @pytest.mark.parametrize("verdict", ["re-dispatch", "skip"])
    async def test_harvested_item_is_not_re_reviewed_on_a_stale_snapshot(
        self, tmp_path, verdict
    ) -> None:
        reviewer = FakeReviewer()
        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a"])
            await _tick(ctx, ls)
            reviewer.release("a", ReviewVerdict(verdict, "r", "", ""))
            await _yield()
            # Stale snapshot: A still in_progress with no in-flight run.
            assert await _tick(ctx, ls) == _TICK_CONTINUE
            await _yield()
            assert reviewer.items() == ["a"]
            assert ls.reviews == {}

    async def test_drain_refusal_and_normal_item_in_one_tick(self, tmp_path) -> None:
        reviewer = FakeReviewer()
        ls = _RolloutLoopState()

        async def _runs(item_id, **_kw):
            if item_id == "refused":
                return [
                    {
                        "rollout_id": ROLLOUT,
                        "status": "failed",
                        "error_msg": "host 'h1' is draining: deploy",
                        "created_at": "2026-10-25T00:00:00",
                    }
                ]
            return []

        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
        ):
            _wire(ctx, tmp_path)
            ctx.gtd.list_runs_for_item = AsyncMock(side_effect=_runs)
            _board(ctx, in_progress=["refused", "normal"])
            assert await _tick(ctx, ls) == _TICK_CONTINUE
            await _yield()
            ctx.gtd.reset_rollout_item.assert_awaited_once_with(
                ROLLOUT, "refused", token=None
            )
            ctx.gtd.dispatch_item.assert_awaited_once_with(
                "refused", rollout_id=ROLLOUT, token=None
            )
            assert reviewer.items() == ["normal"]
            assert ls.reviews["normal"].slot == 0
            await _cleanup(ls)

    async def test_unrecorded_merge_never_gets_a_review(self, tmp_path) -> None:
        reviewer = FakeReviewer()
        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
            patch.object(main, "_monotonic", return_value=0.0),
        ):
            _wire(ctx, tmp_path)
            ctx.unrecorded[ROLLOUT] = {"a": main._UnrecordedMerge(1, "n", 1e9)}
            _board(ctx, in_progress=["a"])
            assert await _tick(ctx, ls) == _TICK_WAIT
            assert reviewer.calls == []
            assert ls.reviews == {}


# ---------------------------------------------------------------------------
# AC5 — early exits wait for an active land
# ---------------------------------------------------------------------------


class TestEarlyExitWaitsForLand:
    @pytest.mark.parametrize(
        ("board", "expected"),
        [({"status": "halted"}, _TICK_HALTED), ({"graph_complete": True}, _TICK_DONE)],
    )
    async def test_exit_waits_then_returns(self, tmp_path, board, expected) -> None:
        release = asyncio.Event()

        async def _land():
            await release.wait()
            return _TICK_CONTINUE

        task = asyncio.create_task(_land())
        ls = _RolloutLoopState(
            slots={0: _ws(tmp_path, 0)}, active_land=_ActiveLand("a", 0, task, 0.0)
        )
        with _loop_patch_stack() as ctx:
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a"], **board)
            assert await _tick(ctx, ls) == _TICK_WAIT
            assert ls.pending_directive == expected
            assert not task.cancelled()
            ctx.gtd.update_rollout_state.assert_not_awaited()
            release.set()
            await task
            assert await _tick(ctx, ls) == expected
        assert not task.cancelled()

    async def test_pending_directive_returned_after_harvest(self, tmp_path) -> None:
        ls = _RolloutLoopState(
            slots={0: _ws(tmp_path, 0)},
            active_land=_ActiveLand("a", 0, _done_task(_TICK_CONTINUE), 0.0),
            pending_directive=_TICK_HALTED,
        )
        await _yield()
        with _loop_patch_stack() as ctx:
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a", "b"], next_ready=["c"])
            assert await _tick(ctx, ls) == _TICK_HALTED
            ctx.gtd.dispatch_item.assert_not_awaited()
        assert ls.pending_directive is None


# ---------------------------------------------------------------------------
# AC6 — harvest branches
# ---------------------------------------------------------------------------


class TestHarvest:
    async def test_raised_review_task_halts(self, tmp_path, caplog) -> None:
        async def _boom():
            raise RuntimeError("kaput")

        task = asyncio.create_task(_boom())
        await _yield()
        ls = _RolloutLoopState(slots={0: _ws(tmp_path, 0)})
        ls.reviews["a"] = _entry("a", 0, task)
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a"])
            assert await _tick(ctx, ls) == _TICK_HALTED
        reason = ctx.gtd.halt_rollout.await_args.kwargs["reason"]
        assert "review task raised" in reason
        assert "RuntimeError: kaput" in reason
        msgs = [r.getMessage() for r in caplog.records]
        assert any("review-task-raised" in m and "slot=0" in m for m in msgs)
        assert any("action=halt-raised" in m for m in msgs)

    async def test_cancelled_review_task_is_dropped(self, tmp_path) -> None:
        task = _pending_task()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        ls = _RolloutLoopState(slots={0: _ws(tmp_path, 0)})
        ls.reviews["a"] = _entry("a", 0, task)
        attempts = {"a": 1}
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_act_on_verdict", new=AsyncMock()) as act,
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a"])
            assert await _tick(ctx, ls, dispatch_attempts=attempts) == _TICK_CONTINUE
        act.assert_not_awaited()
        assert ls.reviews == {}
        assert attempts == {}

    async def test_transient_land_error_frees_the_slot(self, tmp_path, caplog) -> None:
        async def _blip():
            raise httpx.ReadTimeout("board down")

        task = asyncio.create_task(_blip())
        await _yield()
        ls = _RolloutLoopState(
            slots={0: _ws(tmp_path, 0)}, active_land=_ActiveLand("a", 0, task, 0.0)
        )
        with (
            _loop_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a"])
            assert await _tick(ctx, ls) == _TICK_CONTINUE
        assert ls.active_land is None
        ctx.gtd.halt_rollout.assert_not_awaited()
        assert any("land-task-transient" in r.getMessage() for r in caplog.records)

    async def test_other_land_error_halts_and_never_requeues(self, tmp_path) -> None:
        async def _boom():
            raise ValueError("bad tree")

        task = asyncio.create_task(_boom())
        await _yield()
        ws = _ws(tmp_path, 0)
        ls = _RolloutLoopState(
            slots={0: ws}, active_land=_ActiveLand("a", 0, task, 0.0)
        )
        with _loop_patch_stack() as ctx:
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a"])
            assert await _tick(ctx, ls) == _TICK_HALTED
        reason = ctx.gtd.halt_rollout.await_args.kwargs["reason"]
        assert "land task for item a raised: ValueError: bad tree" in reason
        assert ls.land_queue == []
        assert ls.active_land is None
        assert ws.root.is_dir()


# ---------------------------------------------------------------------------
# AC7 / AC15 — serialized land
# ---------------------------------------------------------------------------


class TestSerializedLand:
    async def test_two_merges_land_one_at_a_time_in_harvest_order(
        self, tmp_path, caplog
    ) -> None:
        reviewer = FakeReviewer()
        in_progress = {"n": 0, "max": 0}
        order: list[str] = []
        gates: dict[str, asyncio.Event] = {}

        async def _land(rollout_id, item_id, verdict, **kw):
            in_progress["n"] += 1
            in_progress["max"] = max(in_progress["max"], in_progress["n"])
            order.append(f"start-{item_id}")
            await gates.setdefault(item_id, asyncio.Event()).wait()
            in_progress["n"] -= 1
            order.append(f"end-{item_id}")
            return verdict

        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
            patch.object(main, "_land_reviewed_merge", new=_land),
            patch.object(main, "_verify_landed_commit_type", new=AsyncMock()),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a", "b"])
            await _tick(ctx, ls)
            merge = ReviewVerdict("merge", "r", "n", "")
            reviewer.release("a", merge)
            reviewer.release("b", merge)
            await _yield()
            await _tick(ctx, ls)  # harvests both; starts ONE land
            await _yield()
            assert order == ["start-a"]
            assert [q.item_id for q in ls.land_queue] == ["b"]
            await _tick(ctx, ls)
            await _yield()
            assert order == ["start-a"]  # still one land
            gates.setdefault("a", asyncio.Event()).set()
            await _yield()
            await _tick(ctx, ls)  # harvests a's land, starts b's
            await _yield()
            gates.setdefault("b", asyncio.Event()).set()
            await _yield()
            await _tick(ctx, ls)
        assert order == ["start-a", "end-a", "start-b", "end-b"]
        assert in_progress["max"] == 1
        completed = [c.args[1] for c in ctx.gtd.complete_in_rollout.await_args_list]
        assert completed == ["a", "b"]
        msgs = [r.getMessage() for r in caplog.records]
        for event in ("review-start", "review-harvest", "land-start", "land-done"):
            assert any(f"rollout-loop: {event} " in m for m in msgs), event
        assert any("land-enqueue" in m and "queue_pos=2" in m for m in msgs)
        assert ls.lands == 2


# ---------------------------------------------------------------------------
# AC8 / AC9 — halt teardown and reviewer kill
# ---------------------------------------------------------------------------


class TestHaltTeardown:
    async def test_halt_verdict_tears_down_reviews_and_drops_queued_lands(
        self, tmp_path, caplog
    ) -> None:
        b_task = _pending_task()
        ls = _RolloutLoopState(slots={i: _ws(tmp_path, i) for i in range(3)})
        ls.reviews["a"] = _entry(
            "a", 0, _done_task(ReviewVerdict("halt", "broken", "", "detail"))
        )
        ls.reviews["b"] = _entry("b", 1, b_task)
        ls.land_queue.append(_queued("c", 2))
        await _yield()
        ls.active_land = None
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_land_reviewed_merge", new=AsyncMock()) as land,
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a", "b", "c"], next_ready=["d"])
            assert await _tick(ctx, ls) == _TICK_HALTED
        ctx.gtd.halt_rollout.assert_awaited_once()
        reason = ctx.gtd.halt_rollout.await_args.kwargs["reason"]
        assert reason.endswith(
            "; discarded queued merge verdict(s) for c: "
            "they will be re-reviewed on resume"
        )
        assert b_task.cancelled()
        land.assert_not_awaited()
        ctx.gtd.dispatch_item.assert_not_awaited()
        assert ls.reviews == {}
        assert ls.land_queue == []
        line = next(
            r.getMessage() for r in caplog.records if "halt-teardown" in r.getMessage()
        )
        assert "cause=verdict cause_item=a" in line
        assert "reviews_cancelled=b" in line
        assert "land_queue_dropped=c" in line
        assert ls.halt_cancelled_reviews == 1

    async def test_halt_with_an_active_land_waits_for_it(self, tmp_path) -> None:
        release = asyncio.Event()

        async def _land():
            await release.wait()
            return _TICK_CONTINUE

        land_task = asyncio.create_task(_land())
        ls = _RolloutLoopState(slots={i: _ws(tmp_path, i) for i in range(2)})
        ls.active_land = _ActiveLand("x", 1, land_task, 0.0)
        ls.reviews["a"] = _entry(
            "a", 0, _done_task(ReviewVerdict("halt", "broken", "", ""))
        )
        await _yield()
        with _loop_patch_stack() as ctx:
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a", "x"])
            assert await _tick(ctx, ls) == _TICK_WAIT
            assert ls.pending_directive == _TICK_HALTED
            assert not land_task.cancelled()
            release.set()
            await land_task
            # The board now reports the rollout halted.
            _board(ctx, in_progress=["a", "x"], status="halted")
            assert await _tick(ctx, ls) == _TICK_HALTED

    async def test_cancelling_a_review_kills_its_reviewer(self, monkeypatch) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        started = asyncio.Event()

        async def _run_agent(*_a, popen_callback=None, **_kw):
            popen_callback(proc)
            started.set()
            await asyncio.Event().wait()

        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_active_subprocesses", {}) as subprocs,
            patch.object(main, "kill_run_session") as kill,
            patch.object(main, "run_session_has_members", return_value=False),
        ):
            ctx.dispatch.run_agent = AsyncMock(side_effect=_run_agent)
            ctx.gtd.list_runs_for_item = AsyncMock(return_value=[])
            ctx.gtd.get_rollout_merge_notes = AsyncMock(return_value=[])
            ws = SimpleNamespace(
                root=Path("/nonexistent-ws"),
                repo_paths={"r": Path("/nonexistent-ws/r")},
                default_branches={"r": "main"},
                workspace_mode=True,
            )
            task = asyncio.create_task(
                main._launch_reviewer(
                    ROLLOUT,
                    "item-a",
                    project={"name": "p"},
                    workspace=ws,
                    reviewer_engine=SimpleNamespace(name="claude-code-sonnet"),
                    token=None,
                    attribution=None,
                    item={"id": "item-a", "title": "t"},
                )
            )
            waiter = asyncio.create_task(started.wait())
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            assert not task.done(), task.exception()
            run_id = ctx.db.insert_run.await_args.args[0].id
            assert subprocs[run_id] is proc
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert kill.call_args_list[0].args == (proc, signal.SIGTERM)
        assert kill.call_args_list[1].args == (proc, signal.SIGKILL)
        kwargs = ctx.db.update_run.await_args.kwargs
        assert kwargs["status"] == RunStatus.cancelled
        assert kwargs["error"] == "reviewer cancelled by rollout loop"
        assert run_id not in subprocs

    async def test_cancel_before_popen_kills_the_late_process(self) -> None:
        """A cancel that lands before the executor thread's Popen registered
        the process: the late callback kills it instead of orphaning it."""
        proc = MagicMock()
        captured: dict[str, Any] = {}
        started = asyncio.Event()

        async def _run_agent(*_a, popen_callback=None, **_kw):
            captured["cb"] = popen_callback
            started.set()
            await asyncio.Event().wait()

        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_active_subprocesses", {}) as subprocs,
            patch.object(main, "kill_run_session") as kill,
            patch.object(main, "run_session_has_members", return_value=False),
        ):
            ctx.dispatch.run_agent = AsyncMock(side_effect=_run_agent)
            ctx.gtd.list_runs_for_item = AsyncMock(return_value=[])
            ctx.gtd.get_rollout_merge_notes = AsyncMock(return_value=[])
            ws = SimpleNamespace(
                root=Path("/nonexistent-ws"),
                repo_paths={"r": Path("/nonexistent-ws/r")},
                default_branches={"r": "main"},
                workspace_mode=True,
            )
            task = asyncio.create_task(
                main._launch_reviewer(
                    ROLLOUT,
                    "item-a",
                    project={"name": "p"},
                    workspace=ws,
                    reviewer_engine=SimpleNamespace(name="claude-code-sonnet"),
                    token=None,
                    attribution=None,
                    item={"id": "item-a", "title": "t"},
                )
            )
            waiter = asyncio.create_task(started.wait())
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            assert not task.done(), task.exception()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            kill.assert_not_called()  # nothing registered yet
            # The executor thread's Popen returns only now.
            await asyncio.to_thread(captured["cb"], proc)
        kill.assert_called_once_with(proc, signal.SIGKILL)
        assert subprocs == {}

    async def test_terminate_run_session_without_escalation(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = 0
        with (
            patch.object(main, "kill_run_session") as kill,
            patch.object(main, "run_session_has_members", return_value=False),
            patch.object(main.asyncio, "sleep", new=AsyncMock()),
        ):
            assert await main._terminate_run_session(proc) is False
        kill.assert_called_once_with(proc, signal.SIGTERM)


# ---------------------------------------------------------------------------
# AC10 — host drain finishes reviews and lands, then parks
# ---------------------------------------------------------------------------


class TestDrainFinishesThenParks:
    async def test_drain_with_a_review_in_flight(self, tmp_path, caplog) -> None:
        reviewer = FakeReviewer()
        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
            patch.object(
                main,
                "_land_reviewed_merge",
                new=AsyncMock(side_effect=lambda _r, _i, v, **_k: v),
            ) as land,
            patch.object(main, "_verify_landed_commit_type", new=AsyncMock()),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a"])
            await _tick(ctx, ls)
            await _yield()
            _board(ctx, in_progress=["a", "b"], next_ready=["c"], drain="deploy")
            assert await _tick(ctx, ls) == _TICK_WAIT
            ctx.gtd.dispatch_item.assert_not_awaited()
            assert reviewer.items() == ["a"]
            reviewer.release("a", ReviewVerdict("merge", "r", "n", ""))
            await _yield()
            assert await _tick(ctx, ls) == _TICK_WAIT  # harvest; land starts
            await _yield()
            land.assert_awaited_once()
            assert await _tick(ctx, ls) == _TICK_PARKED
        ctx.gtd.dispatch_item.assert_not_awaited()
        assert reviewer.items() == ["a"]
        msgs = [r for r in caplog.records if "drain-finishing" in r.getMessage()]
        assert msgs[0].levelno == logging.INFO
        assert all(m.levelno == logging.DEBUG for m in msgs[1:])
        steps = [
            c.kwargs.get("current_step") or ""
            for c in ctx.gtd.update_rollout_state.await_args_list
        ]
        assert any(
            s == "Host draining: finishing 1 review(s) and 0 land(s) before parking"
            for s in steps
        )


# ---------------------------------------------------------------------------
# AC12 — restart: already-landed precheck
# ---------------------------------------------------------------------------


class TestPreReviewLandedCheck:
    async def test_fresh_state_reviews_a_terminal_build(self, tmp_path) -> None:
        reviewer = FakeReviewer()
        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a"])
            await _tick(ctx, ls)
            await _yield()
            assert reviewer.items() == ["a"]
            kw = ctx.dispatch.find_landed_commit_subjects.call_args.kwargs
            assert kw["rollout_id"] == ROLLOUT
            await _cleanup(ls)

    async def test_full_hit_records_without_a_reviewer(self, tmp_path, caplog) -> None:
        reviewer = FakeReviewer()
        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            ctx.dispatch.find_landed_commit_subjects = MagicMock(
                return_value=({"repo-a": "feat(a): X"}, [])
            )
            _board(ctx, in_progress=["a"])
            assert await _tick(ctx, ls) == _TICK_CONTINUE
            await _yield()
        assert reviewer.calls == []
        kw = ctx.gtd.complete_in_rollout.await_args.kwargs
        assert kw["outcome"] == "completed"
        assert kw["merge_note"] == (
            "Recorded after restart: the squash commit for a was already on "
            "origin (repo-a: feat(a): X)."
        )
        line = next(
            r for r in caplog.records if "pre-review-landed-check" in r.getMessage()
        )
        assert line.levelno == logging.WARNING
        assert "action=recorded" in line.getMessage()

    async def test_record_failure_routes_to_unrecorded_merge(self, tmp_path) -> None:
        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=FakeReviewer()) as reviewer,
            patch.object(
                main, "_handle_unrecorded_merge", new=AsyncMock(return_value="continue")
            ) as handle,
            patch.object(main, "_BOOKKEEPING_RETRY_DELAYS_SECONDS", ()),
            patch.object(main, "_BOOKKEEPING_ATTEMPTS", 1),
        ):
            _wire(ctx, tmp_path)
            ctx.dispatch.find_landed_commit_subjects = MagicMock(
                return_value=({"repo-a": "feat(a): X"}, [])
            )
            ctx.gtd.complete_in_rollout = AsyncMock(side_effect=_http_error(500))
            _board(ctx, in_progress=["a"])
            assert await _tick(ctx, ls) == _TICK_CONTINUE
        handle.assert_awaited_once()
        verdict = handle.await_args.args[2]
        assert verdict.verdict == "merge"
        assert verdict.rationale == "already landed on origin"
        assert reviewer.calls == []

    async def test_monorepo_hit_records(self, tmp_path) -> None:
        mono = SimpleNamespace(
            root=tmp_path,
            repo_paths={"mono": tmp_path},
            default_branches={"mono": "main"},
            workspace_mode=False,
        )
        ls = _RolloutLoopState(slots={0: mono})
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=FakeReviewer()) as reviewer,
        ):
            _wire(ctx, tmp_path)
            ctx.dispatch.find_landed_commit_subjects = MagicMock(
                return_value=({"mono": "fix(a): Y"}, [])
            )
            ctx.gtd.complete_in_rollout = AsyncMock(
                return_value={"newly_ready": [], "graph_complete": True}
            )
            _board(ctx, in_progress=["a"])
            assert await _tick(ctx, ls) == _TICK_DONE
        assert reviewer.calls == []

    async def test_workspace_hit_in_only_the_touched_repo_records(
        self, tmp_path
    ) -> None:
        """A one-repo item in a two-repo workspace lands in that repo only."""
        two = _ws(tmp_path, 0, repos=("repo-a", "repo-b"))
        ls = _RolloutLoopState(slots={0: two})
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=FakeReviewer()) as reviewer,
        ):
            _wire(ctx, tmp_path)
            ctx.dispatch.find_landed_commit_subjects = MagicMock(
                return_value=({"repo-a": "feat(a): X"}, [])
            )
            _board(ctx, in_progress=["a"])
            assert await _tick(ctx, ls) == _TICK_CONTINUE
        ctx.gtd.halt_rollout.assert_not_awaited()
        kw = ctx.gtd.complete_in_rollout.await_args.kwargs
        assert kw["outcome"] == "completed"
        assert "repo-a: feat(a): X" in kw["merge_note"]
        assert reviewer.calls == []

    async def test_hit_with_a_failed_repo_lookup_halts(self, tmp_path) -> None:
        two = _ws(tmp_path, 0, repos=("repo-a", "repo-b"))
        ls = _RolloutLoopState(slots={0: two})
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=FakeReviewer()) as reviewer,
        ):
            _wire(ctx, tmp_path)
            ctx.dispatch.find_landed_commit_subjects = MagicMock(
                return_value=({"repo-a": "feat(a): X"}, ["repo-b"])
            )
            _board(ctx, in_progress=["a"])
            assert await _tick(ctx, ls) == _TICK_HALTED
        ctx.gtd.complete_in_rollout.assert_not_awaited()
        reason = ctx.gtd.halt_rollout.await_args.kwargs["reason"]
        assert "on origin in repo-a" in reason
        assert "lookup failed in repo-b" in reason
        assert reviewer.calls == []

    async def test_halt_behind_an_active_land_stops_the_tick(
        self, tmp_path, caplog
    ) -> None:
        """A precheck halt while a land runs starts no review and no dispatch."""
        release = asyncio.Event()

        async def _land():
            await release.wait()
            return _TICK_CONTINUE

        land_task = asyncio.create_task(_land())
        ls = _RolloutLoopState(slots={2: _ws(tmp_path, 2)})
        ls.active_land = _ActiveLand("x", 2, land_task, 0.0)
        reviewer = FakeReviewer()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            ctx.dispatch.find_landed_commit_subjects = MagicMock(
                side_effect=lambda _ws, item_id, **_kw: (
                    ({"repo-a": "feat(a): X"}, ["repo-b"])
                    if item_id == "a"
                    else ({}, [])
                )
            )
            _board(ctx, in_progress=["a", "b", "x"], next_ready=["c"])
            assert await _tick(ctx, ls) == _TICK_WAIT
            await _yield()
            assert ls.pending_directive == _TICK_HALTED
            assert reviewer.calls == []
            assert ls.reviews == {}
            ctx.gtd.dispatch_item.assert_not_awaited()
            ctx.gtd.halt_rollout.assert_awaited_once()
            assert not land_task.cancelled()
            release.set()
            await land_task
        teardowns = [r for r in caplog.records if "halt-teardown" in r.getMessage()]
        assert len(teardowns) == 1

    async def test_plain_halt_tears_down_once(self, tmp_path, caplog) -> None:
        two = _ws(tmp_path, 0, repos=("repo-a", "repo-b"))
        ls = _RolloutLoopState(slots={0: two})
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=FakeReviewer()),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            ctx.dispatch.find_landed_commit_subjects = MagicMock(
                return_value=({"repo-a": "feat(a): X"}, ["repo-b"])
            )
            _board(ctx, in_progress=["a"], next_ready=["c"])
            assert await _tick(ctx, ls) == _TICK_HALTED
            ctx.gtd.dispatch_item.assert_not_awaited()
        teardowns = [r for r in caplog.records if "halt-teardown" in r.getMessage()]
        assert len(teardowns) == 1


# ---------------------------------------------------------------------------
# AC13 — phase publishing
# ---------------------------------------------------------------------------


class TestPhasePublishing:
    async def test_step_names_every_concurrent_review(self, tmp_path) -> None:
        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=FakeReviewer()),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["item-a", "item-b"])
            await _tick(ctx, ls)
            call = ctx.gtd.update_rollout_state.await_args
            await _cleanup(ls)
        assert call.args[1] == "reviewing"
        assert call.kwargs["current_step"] == (
            "Reviewing 2 completed build(s): item-a, item-b; 0 queued to land"
        )
        assert call.kwargs["current_item_id"] == "item-b"

    async def test_nothing_published_while_a_land_runs(self, tmp_path) -> None:
        ls = _RolloutLoopState(slots={0: _ws(tmp_path, 0)})
        ls.active_land = _ActiveLand("x", 0, _pending_task(), 0.0)
        with _loop_patch_stack() as ctx:
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["x"], next_ready=["c"])
            assert await _tick(ctx, ls) == _TICK_CONTINUE
            ctx.gtd.dispatch_item.assert_awaited_once()
            ctx.gtd.update_rollout_state.assert_not_awaited()
            await _cleanup(ls)


# ---------------------------------------------------------------------------
# AC14 — invariant audit
# ---------------------------------------------------------------------------


class TestInvariantAudit:
    @staticmethod
    def _violations(caplog) -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.ERROR
            and "concurrency-invariant-violation" in r.getMessage()
        ]

    async def _halts(self, tmp_path, caplog, ls, kind) -> None:
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_land_reviewed_merge", new=AsyncMock()),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            _board(ctx, in_progress=["a", "b"], in_flight=["a", "b"])
            await _tick(ctx, ls)
            await _cleanup(ls)
        assert any(f"kind={kind}" in m for m in self._violations(caplog))
        assert ctx.gtd.halt_rollout.await_args.kwargs["reason"].startswith(
            f"bad_local_state: {kind}"
        )

    async def test_reviews_over_limit(self, tmp_path, caplog, monkeypatch) -> None:
        monkeypatch.setattr(config, "ROLLOUT_MAX_CONCURRENT_REVIEWS", 1)
        ls = _RolloutLoopState()
        ls.reviews["a"] = _entry("a", 0, _pending_task())
        ls.reviews["b"] = _entry("b", 1, _pending_task())
        await self._halts(tmp_path, caplog, ls, "reviews-over-limit")

    async def test_slot_shared(self, tmp_path, caplog) -> None:
        ls = _RolloutLoopState()
        ls.reviews["a"] = _entry("a", 0, _pending_task())
        ls.reviews["b"] = _entry("b", 0, _pending_task())
        await self._halts(tmp_path, caplog, ls, "slot-shared")

    async def test_land_paused_inside_unrecorded_merge_is_not_double_tracked(
        self, tmp_path, caplog
    ) -> None:
        """Real interleaving: the land task registers an unrecorded merge, then
        blocks on its warning comment while another review's harvest ticks."""
        reviewer = FakeReviewer()
        comment_gate = asyncio.Event()
        in_comment = asyncio.Event()

        async def _land(rollout_id, item_id, verdict, **kw):
            return verdict

        async def _comment(item_id, body, **_kw):
            if "merged this item" in body:
                in_comment.set()
                await comment_gate.wait()

        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
            patch.object(main, "_land_reviewed_merge", new=_land),
            patch.object(main, "_verify_landed_commit_type", new=AsyncMock()),
            patch.object(main, "_BOOKKEEPING_RETRY_DELAYS_SECONDS", ()),
            patch.object(main, "_BOOKKEEPING_ATTEMPTS", 1),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            ctx.gtd.post_comment = AsyncMock(side_effect=_comment)

            async def _complete(rollout_id, item_id, **_kw):
                if item_id == "a":
                    raise _http_error(500)
                return {"newly_ready": []}

            ctx.gtd.complete_in_rollout = AsyncMock(side_effect=_complete)
            _board(ctx, in_progress=["a", "b"])
            await _tick(ctx, ls)  # starts reviews of a and b
            await _yield()
            reviewer.release("a", ReviewVerdict("merge", "r", "n", ""))
            await _yield()
            await _tick(ctx, ls)  # harvests a, starts its land
            # The land's record fails, the merge is confirmed on origin, the
            # item is registered as unrecorded, and the land blocks on its
            # warning comment.  find_landed_commit_subjects now reports a hit.
            ctx.dispatch.find_landed_commit_subjects = MagicMock(
                return_value=({"repo-a": "feat(a): X"}, [])
            )
            await asyncio.wait_for(in_comment.wait(), 5)
            assert ls.active_land is not None and ls.active_land.item_id == "a"
            assert "a" in ctx.unrecorded[ROLLOUT]
            # Another review finishes and triggers a tick in that window.
            reviewer.release("b", ReviewVerdict("skip", "r", "", ""))
            await _yield()
            directive = await _tick(ctx, ls)
            assert directive != _TICK_HALTED
            ctx.gtd.halt_rollout.assert_not_awaited()
            assert self._violations(caplog) == []
            comment_gate.set()
            await _yield()
            assert ls.active_land is not None and ls.active_land.task.done()
            assert await _tick(ctx, ls) != _TICK_HALTED
            ctx.gtd.halt_rollout.assert_not_awaited()
            await _cleanup(ls)
        assert self._violations(caplog) == []

    async def test_item_double_tracked(self, tmp_path, caplog) -> None:
        ls = _RolloutLoopState(slots={1: _ws(tmp_path, 1)})
        ls.reviews["a"] = _entry("a", 0, _pending_task())
        ls.land_queue.append(_queued("a", 1))
        await self._halts(tmp_path, caplog, ls, "item-double-tracked")

    def test_second_land(self, caplog) -> None:
        ls = _RolloutLoopState()
        ls.active_land = _ActiveLand("x", 0, MagicMock(), 0.0)
        ls.land_queue.append(_queued("y", 1))
        with caplog.at_level(logging.INFO, logger=LOGGER):
            kind = main._audit_loop_state(ROLLOUT, ls, land_start=True)
        assert kind == "second-land"
        assert any("kind=second-land" in m for m in self._violations(caplog))

    def test_dispatch_while_tracked(self, caplog) -> None:
        ls = _RolloutLoopState()
        ls.reviews["a"] = _entry("a", 0, MagicMock())
        with caplog.at_level(logging.INFO, logger=LOGGER):
            kind = main._audit_loop_state(ROLLOUT, ls, dispatching="a")
        assert kind == "dispatch-while-tracked"
        assert any("kind=dispatch-while-tracked" in m for m in self._violations(caplog))

    def test_clean_state_passes(self) -> None:
        ls = _RolloutLoopState()
        ls.reviews["a"] = _entry("a", 0, MagicMock())
        ls.land_queue.append(_queued("b", 1))
        assert main._audit_loop_state(ROLLOUT, ls, dispatching="c") is None


# ---------------------------------------------------------------------------
# AC16 — drive loop wake, finally and slot cleanup
# ---------------------------------------------------------------------------


def _loop_run() -> Run:
    return Run(project_name="p", mode="manage", rollout_id=ROLLOUT, engine="x")


def _drive_board(ctx, *, done) -> None:
    """A board whose single item stays in progress until ``done()``."""
    ctx.gtd.get_project = AsyncMock(return_value={"name": "p", "workspace_repos": []})

    async def _advance(*_a, **_kw):
        return {
            "next_ready": [],
            "in_progress": [] if done() else ["a"],
            "blocked": [],
            "graph_complete": done(),
        }

    ctx.gtd.advance_rollout = AsyncMock(side_effect=_advance)
    ctx.gtd.get_rollout = AsyncMock(
        return_value={
            "project_id": "proj",
            "status": "running",
            "inFlightBuildRuns": [],
        }
    )


class TestDriveRollout:
    async def test_a_finishing_review_wakes_the_loop(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(config, "ROLLOUT_LOOP_POLL_SECONDS", 30)
        reviewer = FakeReviewer()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
        ):
            _wire(ctx, tmp_path)
            _drive_board(ctx, done=lambda: ctx.gtd.complete_in_rollout.await_count > 0)
            asyncio.get_running_loop().call_later(0.05, reviewer.release, "a")
            result = await asyncio.wait_for(main._drive_rollout(_loop_run()), 1.0)
        assert result == _TICK_DONE
        assert ctx.gtd.complete_in_rollout.await_args.kwargs["outcome"] == "skipped"

    async def test_cancelling_the_drive_cancels_its_reviews(self, tmp_path) -> None:
        reviewer = FakeReviewer()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
        ):
            _wire(ctx, tmp_path)
            _drive_board(ctx, done=lambda: False)
            drive = asyncio.create_task(main._drive_rollout(_loop_run()))
            # The tick's workspace calls run in threads: wait in real time.
            await asyncio.wait_for(reviewer.called.wait(), 2.0)
            assert reviewer.items() == ["a"]
            drive.cancel()
            with pytest.raises(asyncio.CancelledError):
                await drive
        assert reviewer.cancelled == ["a"]
        assert main._host_reviews_in_flight == 0

    @pytest.mark.parametrize(
        ("directive", "cleaned"), [(_TICK_DONE, 2), (_TICK_HALTED, 0)]
    )
    async def test_slot_cleanup(self, tmp_path, directive, cleaned) -> None:
        ls = _RolloutLoopState(slots={0: _ws(tmp_path, 0), 1: _ws(tmp_path, 1)})
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_RolloutLoopState", return_value=ls),
            patch.object(
                main, "_rollout_wave_tick", new=AsyncMock(return_value=directive)
            ),
        ):
            ctx.gtd.get_project = AsyncMock(return_value={"name": "p"})
            ctx.gtd.get_rollout = AsyncMock(return_value={"project_id": "proj"})
            ctx.dispatch.reap_workspace_processes = MagicMock(return_value=[])
            assert await main._drive_rollout(_loop_run()) == directive
        roots = [c.args[0] for c in ctx.dispatch.cleanup_workspace.call_args_list]
        assert len(roots) == cleaned
        if cleaned:
            assert set(roots) == {tmp_path / "slot-0", tmp_path / "slot-1"}
            assert ctx.dispatch.reap_workspace_processes.call_count == 2

    async def test_graph_complete_mid_land_awaits_the_land_then_cleans(
        self, tmp_path
    ) -> None:
        order: list[str] = []
        release = asyncio.Event()

        async def _land():
            await release.wait()
            order.append("land-finished")
            return _TICK_DONE

        land_task = asyncio.create_task(_land())
        ls = _RolloutLoopState(slots={0: _ws(tmp_path, 0)})
        ls.active_land = _ActiveLand("a", 0, land_task, 0.0)
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_RolloutLoopState", return_value=ls),
        ):
            _wire(ctx, tmp_path)
            _drive_board(ctx, done=lambda: True)
            ctx.dispatch.reap_workspace_processes = MagicMock(return_value=[])
            ctx.dispatch.cleanup_workspace = MagicMock(
                side_effect=lambda _root: order.append("cleanup")
            )
            asyncio.get_running_loop().call_later(0.05, release.set)
            result = await asyncio.wait_for(main._drive_rollout(_loop_run()), 1.0)
        assert result == _TICK_DONE
        assert not land_task.cancelled()
        assert order == ["land-finished", "cleanup"]


# ---------------------------------------------------------------------------
# AC17 — dependent ordering
# ---------------------------------------------------------------------------


class TestDependentOrdering:
    async def test_dependent_dispatched_only_after_its_predecessor_lands(
        self, tmp_path
    ) -> None:
        """A dependent is never dispatched against a base missing its predecessor.

        ``advance_rollout`` lists an item in ``next_ready`` only when every
        predecessor's rollout item is terminal (completed, halted or skipped),
        and the worker records ``completed`` for a merge only inside
        ``_act_on_verdict``, AFTER ``_land_reviewed_merge`` has pushed the gated
        sha.  So dispatching ``next_ready`` on every tick, while unrelated
        reviews run, still dispatches a dependent only once its merged
        predecessor's gated sha is on origin's tip.
        """
        reviewer = FakeReviewer()
        calls: list[str] = []

        async def _land(rollout_id, item_id, verdict, **_kw):
            calls.append(f"land-returned-{item_id}")
            return verdict

        async def _complete(rollout_id, item_id, **_kw):
            calls.append(f"complete-{item_id}")
            return {"newly_ready": ["b"]}

        async def _dispatch(item_id, **_kw):
            calls.append(f"dispatch-{item_id}")
            return {"id": f"run-{item_id}"}

        def _advance_now() -> dict[str, Any]:
            a_done = "complete-a" in calls
            dispatched = {
                c.removeprefix("dispatch-") for c in calls if "dispatch-" in c
            }
            return {
                "next_ready": [
                    i
                    for i in (["c"] + (["b"] if a_done else []))
                    if i not in dispatched
                ],
                "in_progress": [
                    i for i in ["a", "c", "b"] if i in dispatched or i == "a"
                ]
                if not a_done
                else sorted(dispatched),
                "blocked": [] if a_done else ["b"],
                "graph_complete": False,
            }

        ls = _RolloutLoopState()
        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
            patch.object(main, "_land_reviewed_merge", new=_land),
            patch.object(main, "_verify_landed_commit_type", new=AsyncMock()),
        ):
            _wire(ctx, tmp_path)
            ctx.gtd.complete_in_rollout = AsyncMock(side_effect=_complete)
            ctx.gtd.dispatch_item = AsyncMock(side_effect=_dispatch)
            ctx.gtd.advance_rollout = AsyncMock(
                side_effect=lambda *_a, **_k: _advance_now()
            )
            ctx.gtd.get_rollout = AsyncMock(
                side_effect=lambda *_a, **_k: {
                    "status": "running",
                    "inFlightBuildRuns": [
                        {"runId": "r", "itemId": i, "status": "running"}
                        for i in ("b", "c")
                    ],
                }
            )
            await _tick(ctx, ls)
            assert calls == ["dispatch-c"]
            assert not ls.reviews["a"].task.done()
            assert ls.dispatches_while_reviewing == 1
            reviewer.release("a", ReviewVerdict("merge", "r", "n", ""))
            await _yield()
            await _tick(ctx, ls)  # harvest A, start its land
            await _yield()
            assert "dispatch-b" not in calls[: calls.index("land-returned-a")]
            await _tick(ctx, ls)  # harvest the land; B is now ready
        assert calls == ["dispatch-c", "land-returned-a", "complete-a", "dispatch-b"]

    async def test_concurrency_summary_logged_on_exit(self, tmp_path, caplog) -> None:
        reviewer = FakeReviewer()
        dispatched: list[str] = []

        async def _dispatch(item_id, **_kw):
            dispatched.append(item_id)
            return {"id": "child"}

        async def _advance(*_a, **_kw):
            done = ctx.gtd.complete_in_rollout.await_count > 0
            return {
                "next_ready": [] if dispatched else ["c"],
                "in_progress": [] if done else ["a", *dispatched],
                "blocked": [],
                "graph_complete": done,
            }

        with (
            _loop_patch_stack() as ctx,
            patch.object(main, "_launch_reviewer", new=reviewer),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            _wire(ctx, tmp_path)
            ctx.gtd.get_project = AsyncMock(return_value={"name": "p"})
            ctx.gtd.dispatch_item = AsyncMock(side_effect=_dispatch)
            ctx.gtd.advance_rollout = AsyncMock(side_effect=_advance)
            ctx.gtd.get_rollout = AsyncMock(
                side_effect=lambda *_a, **_k: {
                    "project_id": "proj",
                    "status": "running",
                    "inFlightBuildRuns": [
                        {"runId": "r", "itemId": i, "status": "running"}
                        for i in dispatched
                    ],
                }
            )
            asyncio.get_running_loop().call_later(0.05, reviewer.release, "a")
            result = await asyncio.wait_for(main._drive_rollout(_loop_run()), 1.0)
        assert result == _TICK_DONE
        summary = next(
            r.getMessage()
            for r in caplog.records
            if "concurrency-summary" in r.getMessage()
        )
        assert "limit=3" in summary
        assert "reviews_started=1" in summary
        assert "dispatches_while_reviewing=1" in summary


# ---------------------------------------------------------------------------
# Misc: helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_with_suffix_keeps_the_suffix_under_the_budget(self) -> None:
        reason = main._with_suffix("x" * 5000, "; tail")
        assert reason.endswith("; tail")
        assert len(reason) == main.ERROR_TEXT_MAX_CHARS

    def test_free_slot_is_the_lowest_unheld(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "ROLLOUT_MAX_CONCURRENT_REVIEWS", 3)
        ls = _RolloutLoopState()
        ls.reviews["a"] = _entry("a", 0, MagicMock())
        ls.active_land = _ActiveLand("b", 2, MagicMock(), 0.0)
        assert main._free_slot(ls) == 1
        ls.land_queue.append(_queued("c", 1))
        assert main._free_slot(ls) is None

    def test_review_pool_is_sized_to_the_limit(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "ROLLOUT_MAX_CONCURRENT_REVIEWS", 4)
        dispatch.init_executor()
        try:
            pool = dispatch._review_executor
            assert isinstance(pool, concurrent.futures.ThreadPoolExecutor)
            assert pool._max_workers == 4
        finally:
            dispatch.init_executor()
