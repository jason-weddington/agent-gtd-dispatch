"""A tearing-down run stays visible to shutdown until its rescue finishes.

A run leaves ``_active_processes`` for ``_tearing_down_tasks`` at the very top
of its teardown ``finally`` and leaves that registry only when evidence capture,
rescue, reaping and cleanup are done.  These tests drive the real
``main._dispatch_worker`` teardown and suspend it deterministically inside the
rescue (two ``asyncio.Event``s, a stubbed executor call) so the shutdown, slot,
retention and audit seams can be observed while a rescue is genuinely in
flight — no polling loops, no sleeps as synchronisation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agent_gtd_dispatch import config, db, dispatch, main, retention, run_tmp
from agent_gtd_dispatch.engines import CLAUDE
from agent_gtd_dispatch.models import DispatchMode, Run
from tests.test_post_run_gate import _pushed
from tests.test_run_tmp import _completed, _Worker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

_REQUIRED_ENV = {
    "DISPATCH_API_KEY": "test-key",
    "AGENT_GTD_URL": "http://localhost:9999",
    "AGENT_GTD_API_KEY": "test-gtd-key",
}
_EXTRA_ENV = (
    "ANTHROPIC_API_KEY",
    "DISPATCH_WORKSPACE_ROOT",
    "DISPATCH_AGENT_SUBPROCESS_USER",
    "DISPATCH_EVIDENCE_ROOT",
)


@pytest.fixture(scope="module", autouse=True)
def _teardown_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Same env as ``tests/test_run_tmp.py::_worker_env``, at module scope.

    Module scope so every test in this file shares one workspace root; restored
    by hand because ``monkeypatch`` is function-scoped.
    """
    saved = {key: os.environ.get(key) for key in (*_REQUIRED_ENV, *_EXTRA_ENV)}
    saved_workspace = config.WORKSPACE_ROOT
    # Other modules leak ids into `_active_processes` (fake dict entries from
    # slot-accounting tests, runs whose TestClient never tears down), so the
    # "fresh registries" invariants these tests assert are meaningless unless this
    # module starts from its own empty dict.  Restored by hand, like the env.
    saved_processes = main._active_processes
    main._active_processes = {}
    os.environ.update(_REQUIRED_ENV)
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
    os.environ["DISPATCH_WORKSPACE_ROOT"] = str(tmp_path_factory.mktemp("teardown-ws"))
    os.environ.pop("DISPATCH_AGENT_SUBPROCESS_USER", None)
    os.environ.pop("DISPATCH_EVIDENCE_ROOT", None)
    config.load()
    yield
    config.WORKSPACE_ROOT = saved_workspace
    main._active_processes = saved_processes
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Rig: the real _dispatch_worker with its world mocked, parked in the rescue
# ---------------------------------------------------------------------------


class _RescueGate:
    """Stub for ``_rescue_before_teardown`` that parks the teardown.

    Sets ``entered`` on entry (the run is now inside its rescue) and waits for
    ``release``, which the test (or a loop callback) sets.  ``finished_wait``
    proves the rescue ran past the wait rather than being thrown away by a
    cancellation.
    """

    def __init__(self, outcome: object = None) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.finished_wait = False
        self._outcome = outcome

    async def stub(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        self.finished_wait = True
        return self._outcome

    def open(self) -> None:
        self.release.set()


@dataclass
class _Suspended:
    run: Run
    task: asyncio.Task[None]
    mock_dispatch: MagicMock
    workspace: Path
    gate: _RescueGate


async def _rescue_none(*_args: Any, **_kwargs: Any) -> None:
    """Rescue stub returning None (nothing to rescue, workspace still cleaned)."""
    return None


@contextlib.contextmanager
def _worker_patches(
    *, rescue_stub: Any = None, pending_worker: Any = None
) -> Iterator[tuple[MagicMock, MagicMock]]:
    """Patch the whole ``dispatch``/``gtd_client`` world the worker talks to."""
    with contextlib.ExitStack() as stack:
        mock_gtd = stack.enter_context(patch("agent_gtd_dispatch.main.gtd_client"))
        mock_dispatch = stack.enter_context(patch("agent_gtd_dispatch.main.dispatch"))
        if rescue_stub is not None:
            stack.enter_context(
                patch(
                    "agent_gtd_dispatch.main._rescue_before_teardown", new=rescue_stub
                )
            )
        if pending_worker is not None:
            stack.enter_context(
                patch("agent_gtd_dispatch.main._dispatch_worker", new=pending_worker)
            )
        yield mock_gtd, mock_dispatch


def _install(
    worker: _Worker,
    mock_gtd: MagicMock,
    mock_dispatch: MagicMock,
    run: Run,
    item: str,
    *,
    cleanup: Any = None,
) -> Path:
    workspace = worker.install(mock_gtd, mock_dispatch, run)
    mock_dispatch.run_agent = AsyncMock(return_value=_completed(0))
    mock_dispatch.verify_pushes = MagicMock(
        return_value=[_pushed(branch=f"feat/{item}")]
    )
    if cleanup is not None:
        mock_dispatch.cleanup_workspace = cleanup
    return workspace


@contextlib.asynccontextmanager
async def _suspended_worker(
    tmp_path: Path,
    item: str,
    *,
    gate: _RescueGate | None = None,
    rescue_stub: Any = None,
    pending_worker: Any = None,
    queue: tuple[Any, ...] = (),
    cleanup: Any = None,
) -> AsyncIterator[_Suspended]:
    """Start a real worker and yield once its teardown is parked in the rescue."""
    gate = gate if gate is not None else _RescueGate()
    stub = rescue_stub if rescue_stub is not None else gate.stub
    real_worker = main._dispatch_worker
    worker = _Worker(tmp_path, item)
    run = await worker.make_run()
    with (
        _worker_patches(rescue_stub=stub, pending_worker=pending_worker) as (
            mock_gtd,
            mock_dispatch,
        ),
        patch.object(main, "_pending_queue", list(queue)),
    ):
        workspace = _install(
            worker, mock_gtd, mock_dispatch, run, item, cleanup=cleanup
        )
        task = asyncio.create_task(real_worker(run, 50, CLAUDE, 600))
        main._active_processes[run.id] = task
        try:
            await asyncio.wait_for(gate.entered.wait(), 10)
            yield _Suspended(
                run=run,
                task=task,
                mock_dispatch=mock_dispatch,
                workspace=workspace,
                gate=gate,
            )
        finally:
            gate.open()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 10)
            main._active_processes.pop(run.id, None)


class _LogSpy:
    """Proxy for one ``logger`` level that signals when ``marker`` is logged.

    Delegating to the real logger keeps ``caplog`` assertions working; the event
    is what makes "that happened already" a deterministic await point instead of
    a sleep.
    """

    def __init__(self, logger: logging.Logger, level: str, marker: str) -> None:
        # Bound BEFORE the patch lands, so delegating does not re-enter the spy.
        self._emit = getattr(logger, level)
        self.marker = marker
        self._signal = asyncio.Event()
        self.hits: list[str] = []

    def __call__(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self.marker in msg:
            self.hits.append(msg % args if args else msg)
            self._signal.set()
        self._emit(msg, *args, **kwargs)

    async def wait(self, count: int = 1) -> None:
        while len(self.hits) < count:
            self._signal.clear()
            await asyncio.wait_for(self._signal.wait(), 10)


def _warning_spy() -> _LogSpy:
    return _LogSpy(main.logger, "warning", "cancellation ignored")


@contextlib.asynccontextmanager
async def _client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        yield http


# ---------------------------------------------------------------------------
# AC-1/AC-2: the registries and the crossing
# ---------------------------------------------------------------------------


class TestRegistries:
    def test_registries_start_empty_and_distinct(self) -> None:
        assert main._tearing_down_tasks == {}
        assert main._teardown_started == {}
        assert main._tearing_down_tasks is not main._active_processes
        assert main._live_workspace_run_ids() == frozenset()

    async def test_suspended_rescue_is_in_the_teardown_registry(
        self, tmp_path: Path
    ) -> None:
        async with _suspended_worker(tmp_path, "i-reg") as s:
            assert s.run.id in main._tearing_down_tasks
            assert s.run.id in main._teardown_started
            assert s.run.id not in main._active_processes
            assert not s.task.done()
            assert main._live_workspace_run_ids() == frozenset({s.run.id})


# ---------------------------------------------------------------------------
# AC-3: leaving the registry on every exit path
# ---------------------------------------------------------------------------


class TestTeardownLeave:
    async def test_cleanup_failure_still_drops_both_registries(
        self, tmp_path: Path
    ) -> None:
        worker = _Worker(tmp_path, "i-boom")
        run = await worker.make_run()
        boom = MagicMock(side_effect=RuntimeError("boom"))
        with _worker_patches(rescue_stub=_rescue_none) as (mock_gtd, mock_dispatch):
            _install(worker, mock_gtd, mock_dispatch, run, "i-boom", cleanup=boom)
            with pytest.raises(RuntimeError, match="boom"):
                await main._dispatch_worker(run, 50, CLAUDE, 600)
        assert main._tearing_down_tasks == {}
        assert main._teardown_started == {}

    async def test_success_path_leaves_no_registry_entries(
        self, tmp_path: Path
    ) -> None:
        worker = _Worker(tmp_path, "i-clean")
        run = await worker.make_run()
        with _worker_patches(rescue_stub=_rescue_none) as (mock_gtd, mock_dispatch):
            _install(worker, mock_gtd, mock_dispatch, run, "i-clean")
            await main._dispatch_worker(run, 50, CLAUDE, 600)
        assert main._tearing_down_tasks == {}
        assert main._teardown_started == {}
        assert run_tmp.live_paths() == frozenset()


# ---------------------------------------------------------------------------
# AC-4: the teardown record
# ---------------------------------------------------------------------------


class TestTeardownRecord:
    async def test_record_reports_an_ignored_cancel(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        spy = _warning_spy()
        with (
            patch.object(main.logger, "warning", new=spy),
            caplog.at_level(logging.INFO),
        ):
            async with _suspended_worker(tmp_path, "i-rec") as s:
                s.task.cancel()
                await spy.wait(1)
                s.gate.open()
                await asyncio.wait_for(s.task, 10)
        recs = [
            r for r in caplog.records if r.getMessage().startswith("teardown: run_id=")
        ]
        assert len(recs) == 1
        message = recs[0].getMessage()
        assert recs[0].levelno == logging.INFO
        assert f"run_id={s.run.id}" in message
        assert "rescue_ok=None" in message
        assert "workspace_kept=False" in message
        assert "cancels_ignored=1" in message
        assert main._tearing_down_tasks == {}

    async def test_record_reports_a_kept_workspace(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        outcome = main._RescueOutcome(
            [
                dispatch.RescueResult(
                    "repos-testproj", attempted=True, pushed=False, committed=True
                )
            ]
        )
        gate = _RescueGate(outcome=outcome)
        with caplog.at_level(logging.INFO):
            async with _suspended_worker(tmp_path, "i-keep", gate=gate) as s:
                assert s.run.id in main._tearing_down_tasks
        s.mock_dispatch.cleanup_workspace.assert_not_called()
        recs = [
            r for r in caplog.records if r.getMessage().startswith("teardown: run_id=")
        ]
        assert len(recs) == 1
        message = recs[0].getMessage()
        assert "rescue_ok=False" in message
        assert "workspace_kept=True" in message


# ---------------------------------------------------------------------------
# AC-5/AC-13: slot accounting and the reported shapes are unchanged
# ---------------------------------------------------------------------------


class TestSlotAccounting:
    async def test_queued_run_promoted_while_a_tears_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 1)
        started = asyncio.Event()
        promoted: list[str] = []

        async def _fake_worker(run: Run, *_args: Any, **_kwargs: Any) -> None:
            promoted.append(run.id)
            started.set()
            main._active_processes.pop(run.id, None)

        queued = Run(
            item_id="i-queued",
            project_name="TestProject",
            branch_name="feat/i-queued",
            mode=DispatchMode.BUILD,
        )
        pending = main._PendingDispatch(queued, CLAUDE, 50, 600, None, enqueued_at=0.0)
        async with _suspended_worker(
            tmp_path, "i-a", pending_worker=_fake_worker, queue=(pending,)
        ) as s:
            assert s.run.id not in main._active_processes
            await asyncio.wait_for(started.wait(), 10)
            assert promoted == [queued.id]
            assert main._pending_queue == []
            assert main._slot_holding_count() == 0
            assert not s.task.done()

    async def test_health_and_info_count_a_teardown_only_host(
        self, tmp_path: Path
    ) -> None:
        async with _suspended_worker(tmp_path, "i-idle") as s:
            async with _client() as http:
                health = (await http.get("/health")).json()
                info = (await http.get("/info")).json()
            assert health["active_runs"] == 0
            assert health["restart_blocking_runs"] == 1
            assert info["active_runs"] == 0
            assert info["slot_runs"] == 0
            assert info["restart_blocking_runs"] == 1
            assert info["restart_blocking"] == {
                "slot_runs": 0,
                "queued_runs": 0,
                "reviews_in_flight": 0,
                "lease_holding_lands": 0,
                "tearing_down_runs": 1,
            }
            assert s.run.id in main._tearing_down_tasks


# ---------------------------------------------------------------------------
# AC-6: the shield helper
# ---------------------------------------------------------------------------


class TestUninterruptible:
    async def test_one_cancel_does_not_abort_the_rescue(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        spy = _warning_spy()
        with (
            patch.object(main.logger, "warning", new=spy),
            caplog.at_level(logging.INFO),
        ):
            async with _suspended_worker(tmp_path, "i-shield") as s:
                s.task.cancel()
                await spy.wait(1)
                s.gate.open()
                await asyncio.wait_for(s.task, 10)
                assert s.gate.finished_wait is True
                expected_tmp = config.RUN_TMP_ROOT / f"rt-{s.run.id}"
                assert not expected_tmp.exists()
                assert expected_tmp not in run_tmp.live_paths()
                s.mock_dispatch.cleanup_workspace.assert_called_once_with(s.workspace)
        recs = [r for r in caplog.records if "cancellation ignored" in r.getMessage()]
        assert len(recs) == 1
        message = recs[0].getMessage()
        assert f"run_id={s.run.id}" in message
        assert "stage=rescue" in message

    async def test_a_second_cancel_logs_a_second_record(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        spy = _warning_spy()
        with (
            patch.object(main.logger, "warning", new=spy),
            caplog.at_level(logging.INFO),
        ):
            async with _suspended_worker(tmp_path, "i-shield2") as s:
                s.task.cancel()
                await spy.wait(1)
                s.task.cancel()
                await spy.wait(2)
                s.gate.open()
                await asyncio.wait_for(s.task, 10)
                assert s.gate.finished_wait is True
        recs = [r for r in caplog.records if "cancellation ignored" in r.getMessage()]
        assert len(recs) == 2

    async def test_uncancelled_path_logs_no_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            async with _suspended_worker(tmp_path, "i-shield3") as s:
                s.gate.open()
                await asyncio.wait_for(s.task, 10)
                assert s.gate.finished_wait is True
        assert not [
            r for r in caplog.records if "cancellation ignored" in r.getMessage()
        ]

    async def test_inner_cancel_reraises_without_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def _self_cancel() -> None:
            current = asyncio.current_task()
            assert current is not None
            current.cancel()
            await asyncio.sleep(0)

        with (
            caplog.at_level(logging.DEBUG),
            pytest.raises(asyncio.CancelledError),
        ):
            await main._await_uninterruptible(_self_cancel(), run_id="r", stage="s")
        assert not [
            r for r in caplog.records if "cancellation ignored" in r.getMessage()
        ]


# ---------------------------------------------------------------------------
# AC-7: the lifespan seam
# ---------------------------------------------------------------------------


class TestLifespanSeam:
    def test_shutdown_helper_awaited_once_and_log_not_duplicated(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from fastapi.testclient import TestClient

        shutdown = AsyncMock()
        with (
            patch("agent_gtd_dispatch.main._readopt_running_rollouts", new=AsyncMock()),
            patch("agent_gtd_dispatch.main._shutdown_active_runs", new=shutdown),
            caplog.at_level(logging.INFO),
            TestClient(main.app),
        ):
            pass
        shutdown.assert_awaited_once()
        recs = [
            r
            for r in caplog.records
            if r.getMessage().startswith("shutdown: restart_blocking_runs=")
        ]
        assert len(recs) == 1


# ---------------------------------------------------------------------------
# AC-8/AC-9: shutdown never re-cancels, waits on both registries, bounded
# ---------------------------------------------------------------------------


class TestShutdownWait:
    async def test_shutdown_waits_for_a_suspended_rescue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "SHUTDOWN_GRACE_SECONDS", 5)
        async with _suspended_worker(tmp_path, "i-race") as s:
            asyncio.get_running_loop().call_later(0.05, s.gate.open)
            await main._shutdown_active_runs()
            assert s.gate.finished_wait is True
            assert s.task.done()
            assert s.task.cancelling() == 0
            assert main._tearing_down_tasks == {}
            assert main._teardown_started == {}
            s.mock_dispatch.cleanup_workspace.assert_called_once_with(s.workspace)

    async def test_bound_expires_without_cancelling_the_teardown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        monkeypatch.setattr(config, "SHUTDOWN_GRACE_SECONDS", 0.05)
        waiting_started = _LogSpy(main.logger, "info", "shutdown: cancelling")
        async with _suspended_worker(tmp_path, "i-bound") as s:
            assert s.task.cancelling() == 0
            started = time.monotonic()
            with (
                patch.object(main.logger, "info", new=waiting_started),
                caplog.at_level(logging.INFO),
            ):
                shutdown = asyncio.create_task(main._shutdown_active_runs())
                # The wait is now in flight: the teardown has not been cancelled.
                await waiting_started.wait()
                assert s.task.cancelling() == 0
                await asyncio.wait_for(shutdown, 10)
            assert time.monotonic() - started < 2
            assert s.task.cancelling() == 0
            assert not s.task.done()
            errors = [
                r
                for r in caplog.records
                if r.levelno == logging.ERROR
                and r.getMessage().startswith(
                    "shutdown: teardown did not finish within"
                )
            ]
            assert len(errors) == 1
            assert s.run.id in errors[0].getMessage()
            stored = await db.get_run(s.run.id)
            assert stored is not None
            assert "teardown_interrupted:" in (stored.error or "")
            s.gate.open()
            await asyncio.wait_for(s.task, 10)
            assert s.task.cancelling() == 0
            assert main._tearing_down_tasks == {}

    async def test_empty_registries_skip_the_wait(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        started = time.monotonic()
        with caplog.at_level(logging.DEBUG):
            await main._shutdown_active_runs()
        assert time.monotonic() - started < 1
        assert not [r for r in caplog.records if r.getMessage().startswith("shutdown:")]

    async def test_in_flight_only_message_is_exact(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(config, "SHUTDOWN_GRACE_SECONDS", 45)
        parked = asyncio.create_task(asyncio.sleep(60))
        monkeypatch.setitem(main._active_processes, "in-flight", parked)
        with caplog.at_level(logging.INFO):
            await main._shutdown_active_runs()
        recs = [
            r
            for r in caplog.records
            if r.getMessage().startswith("shutdown: cancelling")
        ]
        assert len(recs) == 1
        assert recs[0].getMessage() == (
            "shutdown: cancelling 1 in-flight run(s) and waiting up to 45s for"
            " evidence capture and work rescue, including 0 run(s) already in"
            " teardown (oldest_s=0.0)"
        )

    async def test_failed_teardown_warns_during_the_wait(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(config, "SHUTDOWN_GRACE_SECONDS", 5)

        async def _boom() -> None:
            await asyncio.sleep(0.01)
            msg = "teardown exploded"
            raise RuntimeError(msg)

        failing = asyncio.create_task(_boom())
        monkeypatch.setitem(main._tearing_down_tasks, "rid-boom", failing)
        monkeypatch.setitem(main._teardown_started, "rid-boom", time.monotonic())
        with caplog.at_level(logging.INFO):
            await main._shutdown_active_runs()
        recs = [
            r
            for r in caplog.records
            if r.getMessage().startswith("shutdown: run teardown raised")
        ]
        assert len(recs) == 1
        with pytest.raises(RuntimeError, match="teardown exploded"):
            await failing


# ---------------------------------------------------------------------------
# AC-10: the runtime tripwire
# ---------------------------------------------------------------------------


class TestAuditTripwire:
    async def test_info_audits_both_registries(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rid = "rid-audit"
        with (
            patch.object(main, "_active_processes", {rid: MagicMock()}),
            patch.object(
                main,
                "_tearing_down_tasks",
                {rid: MagicMock(done=MagicMock(return_value=True))},
            ),
            caplog.at_level(logging.ERROR),
        ):
            async with _client() as http:
                resp = await http.get("/info")
        assert resp.status_code == 200
        assert f"audit: run in both registries run={rid}" in caplog.text
        assert "audit: teardown registry leak" in caplog.text

    async def test_empty_teardown_registry_is_silent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch.object(main, "_tearing_down_tasks", {}),
            caplog.at_level(logging.ERROR),
        ):
            async with _client() as http:
                await http.get("/info")
        assert "audit: teardown registry leak" not in caplog.text
        assert "audit: run in both registries" not in caplog.text


# ---------------------------------------------------------------------------
# AC-11: prune protection
# ---------------------------------------------------------------------------


class TestPruneProtection:
    def test_prune_keeps_a_tearing_down_runs_workspace(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        rid = "rid-teardown"
        monkeypatch.setattr(main, "_active_processes", {})
        monkeypatch.setattr(main, "_tearing_down_tasks", {rid: MagicMock()})
        monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
        workspace = tmp_path / f"repos-{rid}"
        workspace.mkdir()
        aged = time.time() - 10 * 86400
        os.utime(workspace, (aged, aged))
        assert main._live_workspace_run_ids() == frozenset({rid})
        with caplog.at_level(logging.INFO):
            retention.prune(main._live_workspace_run_ids())
        assert workspace.is_dir()
        assert "retention: skipped active-run path" in caplog.text

    async def test_retention_tick_passes_teardown_ids_to_prune(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rid = "rid-tick"
        monkeypatch.setattr(main, "_active_processes", {})
        monkeypatch.setattr(main, "_tearing_down_tasks", {rid: MagicMock()})
        prune = MagicMock()
        sweep = MagicMock()
        monkeypatch.setattr(retention, "prune", prune)
        monkeypatch.setattr(run_tmp, "sweep_orphans", sweep)
        await main._retention_tick()
        prune.assert_called_once_with(frozenset({rid}))
        assert sweep.call_count == 1
