"""Tests for manage subprocess auto-recovery logic."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_gtd_dispatch.main import (
    MANAGE_RETRY_BACKOFF_SECONDS,
    MAX_MANAGE_RETRIES,
    _maybe_relaunch_manage,
)
from agent_gtd_dispatch.models import Run


def _make_run(mode: str = "manage", rollout_id: str = "rollout-abc") -> Run:
    """Build a minimal Run for testing."""
    return Run(
        item_id="item-123",
        project_name="test-project",
        mode=mode,
        rollout_id=rollout_id,
        engine="claude-code",
    )


def _rollout(
    status: str,
    retry_count: int = 0,
    *,
    manager_phase: str | None = None,
    manager_current_step: str | None = None,
    manager_state_updated_at: str | None = None,
    in_flight: list[dict] | None = None,
) -> dict:
    rollout: dict = {
        "id": "rollout-abc",
        "status": status,
        "manage_retry_count": retry_count,
        "project_id": "proj-1",
    }
    if manager_phase is not None:
        rollout["manager_phase"] = manager_phase
    if manager_current_step is not None:
        rollout["manager_current_step"] = manager_current_step
    if manager_state_updated_at is not None:
        rollout["manager_state_updated_at"] = manager_state_updated_at
    if in_flight is not None:
        rollout["inFlightBuildRuns"] = in_flight
    return rollout


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
# Clean-exit statuses: no relaunch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    ["completed", "halted", "cancelled"],
)
async def test_clean_exit_does_not_relaunch(status: str) -> None:
    """All three clean-exit statuses should result in no relaunch."""
    run = _make_run()
    engine = MagicMock()

    with patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd:
        mock_gtd.get_rollout = AsyncMock(return_value=_rollout(status))
        mock_gtd.relaunch_manage_rollout = AsyncMock()
        mock_gtd.halt_rollout = AsyncMock()

        await _maybe_relaunch_manage(
            run,
            100,
            engine,
            3600,
            None,
            manager_uptime_seconds=1800.0,
            run_timed_out=False,
        )

        mock_gtd.get_rollout.assert_called_once_with(run.rollout_id)
        mock_gtd.relaunch_manage_rollout.assert_not_called()
        mock_gtd.halt_rollout.assert_not_called()


# ---------------------------------------------------------------------------
# Unexpected-exit statuses: relaunch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["running", "pending"])
async def test_unexpected_exit_triggers_relaunch(status: str) -> None:
    """running and pending rollout statuses should trigger a relaunch."""
    run = _make_run()
    engine = MagicMock()

    with (
        patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
        patch("agent_gtd_dispatch.main.db") as mock_db,
        patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()) as mock_sleep,
        patch("agent_gtd_dispatch.main.asyncio.create_task") as mock_create_task,
        patch("agent_gtd_dispatch.main._active_processes", {}),
    ):
        mock_gtd.get_rollout = AsyncMock(return_value=_rollout(status))
        mock_gtd.relaunch_manage_rollout = AsyncMock(
            return_value=_rollout(status, retry_count=1)
        )
        mock_gtd.halt_rollout = AsyncMock()
        mock_db.insert_run = AsyncMock()
        mock_create_task.return_value = MagicMock()

        await _maybe_relaunch_manage(
            run,
            100,
            engine,
            3600,
            None,
            manager_uptime_seconds=1800.0,
            run_timed_out=False,
        )

        mock_gtd.relaunch_manage_rollout.assert_called_once_with(run.rollout_id)
        mock_sleep.assert_called_once_with(MANAGE_RETRY_BACKOFF_SECONDS)
        mock_db.insert_run.assert_called_once()
        mock_create_task.assert_called_once()
        mock_gtd.halt_rollout.assert_not_called()


# ---------------------------------------------------------------------------
# Retry cap: halt_rollout called, no new task
# ---------------------------------------------------------------------------


async def test_retry_cap_exceeded_halts_rollout() -> None:
    """When retry count > MAX_MANAGE_RETRIES, halt the rollout instead of relaunching."""
    run = _make_run()
    engine = MagicMock()

    # retry_count becomes MAX_MANAGE_RETRIES + 1 after increment
    exceeded_count = MAX_MANAGE_RETRIES + 1

    with (
        patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
        patch("agent_gtd_dispatch.main.db") as mock_db,
        patch("agent_gtd_dispatch.main.asyncio.create_task") as mock_create_task,
    ):
        mock_gtd.get_rollout = AsyncMock(return_value=_rollout("running"))
        mock_gtd.relaunch_manage_rollout = AsyncMock(
            return_value=_rollout("running", retry_count=exceeded_count)
        )
        mock_gtd.halt_rollout = AsyncMock()
        mock_db.insert_run = AsyncMock()

        await _maybe_relaunch_manage(
            run,
            100,
            engine,
            3600,
            None,
            manager_uptime_seconds=1800.0,
            run_timed_out=False,
        )

        mock_gtd.halt_rollout.assert_called_once_with(
            run.rollout_id, reason="manage_relaunch_cap_exceeded"
        )
        mock_create_task.assert_not_called()
        mock_db.insert_run.assert_not_called()


# ---------------------------------------------------------------------------
# Exactly at cap: retry_count == MAX_MANAGE_RETRIES should still relaunch
# ---------------------------------------------------------------------------


async def test_at_cap_still_relaunches() -> None:
    """retry_count == MAX_MANAGE_RETRIES is still within budget — should relaunch."""
    run = _make_run()
    engine = MagicMock()

    with (
        patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
        patch("agent_gtd_dispatch.main.db") as mock_db,
        patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()),
        patch("agent_gtd_dispatch.main.asyncio.create_task") as mock_create_task,
        patch("agent_gtd_dispatch.main._active_processes", {}),
    ):
        mock_gtd.get_rollout = AsyncMock(return_value=_rollout("running"))
        mock_gtd.relaunch_manage_rollout = AsyncMock(
            return_value=_rollout("running", retry_count=MAX_MANAGE_RETRIES)
        )
        mock_gtd.halt_rollout = AsyncMock()
        mock_db.insert_run = AsyncMock()
        mock_create_task.return_value = MagicMock()

        await _maybe_relaunch_manage(
            run,
            100,
            engine,
            3600,
            None,
            manager_uptime_seconds=1800.0,
            run_timed_out=False,
        )

        mock_create_task.assert_called_once()
        mock_gtd.halt_rollout.assert_not_called()


# ---------------------------------------------------------------------------
# Human cancellation: _dispatch_worker sets _human_cancelled=True → no relaunch
# ---------------------------------------------------------------------------


async def test_human_cancellation_skips_relaunch() -> None:
    """When the dispatch task is cancelled by a human, no relaunch occurs."""
    from agent_gtd_dispatch import db
    from agent_gtd_dispatch.engines import CLAUDE
    from agent_gtd_dispatch.main import _dispatch_worker
    from agent_gtd_dispatch.models import Run, RunStatus

    await db.init_db()
    run = Run(
        item_id="item-human-cancel",
        project_name="test-project",
        mode="manage",
        rollout_id="rollout-abc",
        engine="claude-code",
    )
    await db.insert_run(run)

    with patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd:
        mock_gtd.get_item = AsyncMock(
            return_value={"id": "item-human-cancel", "title": "T", "project_id": "p1"}
        )
        mock_gtd.get_project = AsyncMock(
            return_value={
                "id": "p1",
                "name": "proj",
                "git_origin": "git@host:repos/repo",
            }
        )
        mock_gtd.get_rollout = AsyncMock(return_value=_rollout("running"))
        mock_gtd.relaunch_manage_rollout = AsyncMock()
        mock_gtd.halt_rollout = AsyncMock()

        with patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch:
            mock_dispatch.prepare_manage_workspace.side_effect = (
                asyncio.CancelledError()
            )
            mock_dispatch.cleanup_workspace = MagicMock()

            # CancelledError is caught inside _dispatch_worker; function returns normally
            await _dispatch_worker(run, 100, CLAUDE, 3600)

        # No relaunch because _human_cancelled was set
        mock_gtd.get_rollout.assert_not_called()
        mock_gtd.relaunch_manage_rollout.assert_not_called()
        mock_gtd.halt_rollout.assert_not_called()

    final = await db.get_run(run.id)
    assert final is not None
    assert final.status == RunStatus.cancelled


# ---------------------------------------------------------------------------
# Build mode: _dispatch_worker does NOT call _maybe_relaunch_manage
# ---------------------------------------------------------------------------


async def test_build_mode_no_relaunch() -> None:
    """Build-mode runs must never trigger manage relaunch logic."""
    from agent_gtd_dispatch import db
    from agent_gtd_dispatch.engines import CLAUDE
    from agent_gtd_dispatch.main import _dispatch_worker
    from agent_gtd_dispatch.models import Run

    await db.init_db()
    run = Run(
        item_id="item-build",
        project_name="test-project",
        mode="build",
        branch_name="feat/item-build-fix",
        engine="claude-code",
    )
    await db.insert_run(run)

    with patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd:
        mock_gtd.get_item = AsyncMock(
            return_value={"id": "item-build", "title": "T", "project_id": "p1"}
        )
        mock_gtd.get_project = AsyncMock(
            return_value={
                "id": "p1",
                "name": "proj",
                "git_origin": "git@host:repos/repo",
            }
        )
        mock_gtd.get_rollout = AsyncMock()
        mock_gtd.relaunch_manage_rollout = AsyncMock()
        mock_gtd.post_comment = AsyncMock()

        with patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch:
            mock_dispatch.prepare_workspace.side_effect = Exception("boom")
            mock_dispatch.cleanup_workspace = MagicMock()
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])

            await _dispatch_worker(run, 100, CLAUDE, 3600)

        # Because mode != "manage" (and no rollout_id), relaunch must not fire
        mock_gtd.get_rollout.assert_not_called()
        mock_gtd.relaunch_manage_rollout.assert_not_called()


# ---------------------------------------------------------------------------
# get_rollout failure: graceful skip
# ---------------------------------------------------------------------------


async def test_get_rollout_failure_skips_recovery() -> None:
    """If get_rollout raises, we log and return without crashing."""
    run = _make_run()
    engine = MagicMock()

    with patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd:
        mock_gtd.get_rollout = AsyncMock(side_effect=Exception("network error"))
        mock_gtd.relaunch_manage_rollout = AsyncMock()
        mock_gtd.halt_rollout = AsyncMock()

        # Should not raise
        await _maybe_relaunch_manage(
            run,
            100,
            engine,
            3600,
            None,
            manager_uptime_seconds=1800.0,
            run_timed_out=False,
        )

        mock_gtd.relaunch_manage_rollout.assert_not_called()
        mock_gtd.halt_rollout.assert_not_called()


# ---------------------------------------------------------------------------
# relaunch_manage_rollout failure: graceful skip
# ---------------------------------------------------------------------------


async def test_relaunch_manage_failure_skips_recovery() -> None:
    """If relaunch_manage_rollout raises, we log and return without crashing."""
    run = _make_run()
    engine = MagicMock()

    with patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd:
        mock_gtd.get_rollout = AsyncMock(return_value=_rollout("running"))
        mock_gtd.relaunch_manage_rollout = AsyncMock(side_effect=Exception("API error"))
        mock_gtd.halt_rollout = AsyncMock()

        await _maybe_relaunch_manage(
            run,
            100,
            engine,
            3600,
            None,
            manager_uptime_seconds=1800.0,
            run_timed_out=False,
        )

        mock_gtd.halt_rollout.assert_not_called()


# ---------------------------------------------------------------------------
# New run attributes in relaunch
# ---------------------------------------------------------------------------


async def test_relaunch_preserves_attribution() -> None:
    """Attribution is forwarded to the relaunched worker."""
    run = _make_run()
    run.agent_name = "my-agent"
    engine = MagicMock()

    spawned_worker_kwargs: dict = {}

    def _capture_create_task(coro) -> MagicMock:
        # Extract kwargs by inspecting the coroutine args
        spawned_worker_kwargs["coro"] = coro
        coro.close()  # prevent "coroutine was never awaited" warning
        return MagicMock()

    with (
        patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
        patch("agent_gtd_dispatch.main.db") as mock_db,
        patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()),
        patch(
            "agent_gtd_dispatch.main.asyncio.create_task",
            side_effect=_capture_create_task,
        ),
        patch("agent_gtd_dispatch.main._active_processes", {}),
    ):
        mock_gtd.get_rollout = AsyncMock(return_value=_rollout("running"))
        mock_gtd.relaunch_manage_rollout = AsyncMock(
            return_value=_rollout("running", retry_count=1)
        )
        mock_db.insert_run = AsyncMock()

        await _maybe_relaunch_manage(
            run,
            100,
            engine,
            3600,
            "my-attribution",
            manager_uptime_seconds=1800.0,
            run_timed_out=False,
        )

        # Worker was spawned (create_task was called)
        assert "coro" in spawned_worker_kwargs


# ---------------------------------------------------------------------------
# Constants sanity check
# ---------------------------------------------------------------------------


def test_constants() -> None:
    """Ensure the recovery constants are set to expected values."""
    assert MAX_MANAGE_RETRIES == 2
    assert MANAGE_RETRY_BACKOFF_SECONDS == 30


# ---------------------------------------------------------------------------
# Free-relaunch exit-path ladder (item 2f5c182f): a manager that exits while
# a healthy child build is still in flight must not burn MAX_MANAGE_RETRIES
# budget. See main.py's _maybe_relaunch_manage decision ladder.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _full_patch_stack():
    """Patch stack for the free-relaunch ladder tests.

    Mirrors this file's own convention (repeated patches per test) and the
    `_watchdog_acted` isolation convention in tests/test_manage_watchdog.py.
    Isolates every module-level dict this feature reads/writes so state never
    leaks across tests, and yields handles for assertions.
    """
    with (
        patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
        patch("agent_gtd_dispatch.main.db") as mock_db,
        patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()) as mock_sleep,
        patch("agent_gtd_dispatch.main.asyncio.create_task") as mock_create_task,
        patch("agent_gtd_dispatch.main._active_processes", {}),
        patch("agent_gtd_dispatch.main._watchdog_acted", {}) as watchdog_acted,
        patch("agent_gtd_dispatch.main._manage_free_relaunches", {}) as free_relaunches,
        patch("agent_gtd_dispatch.main._manage_last_terminal_count", {}),
        patch("agent_gtd_dispatch.main._manage_seen_in_flight_item_ids", {}),
    ):

        def _close_coro_and_mock(coro: object) -> MagicMock:
            # asyncio.create_task is mocked, so the coroutine it's handed
            # (_dispatch_worker(...)) never actually runs — close it to avoid
            # a "coroutine was never awaited" RuntimeWarning at GC time (same
            # pattern as test_relaunch_preserves_attribution above).
            with contextlib.suppress(AttributeError):
                coro.close()  # type: ignore[attr-defined]
            return MagicMock()

        mock_create_task.side_effect = _close_coro_and_mock
        yield SimpleNamespace(
            gtd=mock_gtd,
            db=mock_db,
            sleep=mock_sleep,
            create_task=mock_create_task,
            watchdog_acted=watchdog_acted,
            free_relaunches=free_relaunches,
        )


class TestExitPathBuildInFlight:
    """Regression coverage for the free-relaunch exit-path ladder."""

    async def test_free_relaunch_build_in_flight(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A manager exit while a healthy child build is in flight relaunches for free."""
        run = _make_run()
        engine = MagicMock()
        rollout = _rollout(
            "running",
            retry_count=MAX_MANAGE_RETRIES,
            manager_phase="polling",
            manager_current_step="Waiting for build run 165efa7c to complete",
            manager_state_updated_at=(
                datetime.now(UTC) - timedelta(seconds=60)
            ).isoformat(),
            in_flight=[{"runId": "165efa7c", "itemId": "item-2", "status": "running"}],
        )

        with (
            _full_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            ctx.gtd.get_rollout = AsyncMock(return_value=rollout)
            ctx.gtd.relaunch_manage_rollout = AsyncMock()
            ctx.gtd.halt_rollout = AsyncMock()
            ctx.gtd.post_comment = AsyncMock()
            ctx.db.insert_run = AsyncMock()

            await _maybe_relaunch_manage(
                run,
                100,
                engine,
                3600,
                None,
                manager_uptime_seconds=1800.0,
                run_timed_out=False,
            )

        ctx.gtd.relaunch_manage_rollout.assert_not_called()
        ctx.gtd.halt_rollout.assert_not_called()
        ctx.db.insert_run.assert_called_once()
        ctx.create_task.assert_called_once()
        ctx.gtd.post_comment.assert_called_once()
        assert ctx.gtd.post_comment.call_args.args[0] == "item-2"
        assert any(
            "165efa7c" in r.getMessage()
            and "decision=free-relaunch-build-in-flight" in r.getMessage()
            for r in caplog.records
        )

    async def test_repeat_past_cap_never_halts(self) -> None:
        """Repeating past MAX_MANAGE_RETRIES + 3 times never halts."""
        run = _make_run()
        engine = MagicMock()
        iterations = MAX_MANAGE_RETRIES + 3

        with _full_patch_stack() as ctx:
            ctx.gtd.relaunch_manage_rollout = AsyncMock()
            ctx.gtd.halt_rollout = AsyncMock()
            ctx.gtd.post_comment = AsyncMock()
            ctx.db.insert_run = AsyncMock()

            for _ in range(iterations):
                ctx.gtd.get_rollout = AsyncMock(
                    return_value=_rollout(
                        "running",
                        manager_phase="polling",
                        manager_state_updated_at=(
                            datetime.now(UTC) - timedelta(seconds=60)
                        ).isoformat(),
                        in_flight=[
                            {"runId": "run-1", "itemId": "item-1", "status": "running"}
                        ],
                    )
                )
                await _maybe_relaunch_manage(
                    run,
                    100,
                    engine,
                    3600,
                    None,
                    manager_uptime_seconds=1800.0,
                    run_timed_out=False,
                )

            ctx.gtd.halt_rollout.assert_not_called()
            assert ctx.gtd.relaunch_manage_rollout.call_count == 0
            assert ctx.free_relaunches["rollout-abc"] == iterations

    async def test_crash_loop_protection_preserved(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Short manager uptime still counts toward the cap (crash-loop protection)."""
        run = _make_run()
        engine = MagicMock()
        rollout = _rollout(
            "running",
            manager_phase="polling",
            manager_state_updated_at=(
                datetime.now(UTC) - timedelta(seconds=60)
            ).isoformat(),
            in_flight=[{"runId": "run-1", "itemId": "item-1", "status": "running"}],
        )

        with (
            _full_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            ctx.gtd.get_rollout = AsyncMock(return_value=rollout)
            ctx.gtd.relaunch_manage_rollout = AsyncMock(
                return_value=_rollout("running", retry_count=MAX_MANAGE_RETRIES + 1)
            )
            ctx.gtd.halt_rollout = AsyncMock()
            ctx.db.insert_run = AsyncMock()

            await _maybe_relaunch_manage(
                run,
                100,
                engine,
                3600,
                None,
                manager_uptime_seconds=5.0,
                run_timed_out=False,
            )

        assert any(
            "decision=counted-short-uptime" in r.getMessage() for r in caplog.records
        )
        ctx.gtd.relaunch_manage_rollout.assert_called_once()
        ctx.gtd.halt_rollout.assert_called_once_with(
            run.rollout_id, reason="manage_relaunch_cap_exceeded"
        )

    async def test_free_cap_exhaustion(self, caplog: pytest.LogCaptureFixture) -> None:
        """A third relaunch past MAX_MANAGE_FREE_RELAUNCHES counts toward the cap."""
        from agent_gtd_dispatch import config

        run = _make_run()
        engine = MagicMock()

        def _waiting_rollout() -> dict:
            return _rollout(
                "running",
                manager_phase="polling",
                manager_state_updated_at=(
                    datetime.now(UTC) - timedelta(seconds=60)
                ).isoformat(),
                in_flight=[{"runId": "run-1", "itemId": "item-1", "status": "running"}],
            )

        with (
            patch.object(config, "MAX_MANAGE_FREE_RELAUNCHES", 2),
            _full_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            ctx.gtd.relaunch_manage_rollout = AsyncMock(
                return_value=_rollout("running", retry_count=1)
            )
            ctx.gtd.halt_rollout = AsyncMock()
            ctx.gtd.post_comment = AsyncMock()
            ctx.db.insert_run = AsyncMock()

            for _ in range(config.MAX_MANAGE_FREE_RELAUNCHES):
                ctx.gtd.get_rollout = AsyncMock(return_value=_waiting_rollout())
                await _maybe_relaunch_manage(
                    run,
                    100,
                    engine,
                    3600,
                    None,
                    manager_uptime_seconds=1800.0,
                    run_timed_out=False,
                )

            caplog.clear()
            ctx.gtd.get_rollout = AsyncMock(return_value=_waiting_rollout())
            await _maybe_relaunch_manage(
                run,
                100,
                engine,
                3600,
                None,
                manager_uptime_seconds=1800.0,
                run_timed_out=False,
            )

        ctx.gtd.relaunch_manage_rollout.assert_called_once()
        warning_msgs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("decision=counted-free-cap-exhausted" in m for m in warning_msgs)

    async def test_absolute_backstop(self, caplog: pytest.LogCaptureFixture) -> None:
        """Age past MANAGE_TIMEOUT_SECONDS counts toward the cap even mid-build."""
        from agent_gtd_dispatch import config

        run = _make_run()
        engine = MagicMock()
        rollout = _rollout(
            "running",
            manager_phase="polling",
            manager_state_updated_at=(
                datetime.now(UTC)
                - timedelta(seconds=config.MANAGE_TIMEOUT_SECONDS + 600)
            ).isoformat(),
            in_flight=[{"runId": "run-1", "itemId": "item-1", "status": "running"}],
        )

        with (
            _full_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            ctx.gtd.get_rollout = AsyncMock(return_value=rollout)
            ctx.gtd.relaunch_manage_rollout = AsyncMock(
                return_value=_rollout("running", retry_count=1)
            )
            ctx.gtd.halt_rollout = AsyncMock()
            ctx.db.insert_run = AsyncMock()

            await _maybe_relaunch_manage(
                run,
                100,
                engine,
                3600,
                None,
                manager_uptime_seconds=1800.0,
                run_timed_out=False,
            )

        assert any(
            "decision=counted-backstop-exceeded" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )
        ctx.gtd.relaunch_manage_rollout.assert_called_once()

    async def test_run_timed_out_counts(self, caplog: pytest.LogCaptureFixture) -> None:
        """run_timed_out=True always counts, regardless of in-flight builds."""
        run = _make_run()
        engine = MagicMock()
        rollout = _rollout(
            "running",
            manager_phase="polling",
            manager_state_updated_at=(
                datetime.now(UTC) - timedelta(seconds=60)
            ).isoformat(),
            in_flight=[{"runId": "run-1", "itemId": "item-1", "status": "running"}],
        )

        with (
            _full_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            ctx.gtd.get_rollout = AsyncMock(return_value=rollout)
            ctx.gtd.relaunch_manage_rollout = AsyncMock(
                return_value=_rollout("running", retry_count=1)
            )
            ctx.gtd.halt_rollout = AsyncMock()
            ctx.db.insert_run = AsyncMock()

            await _maybe_relaunch_manage(
                run,
                100,
                engine,
                3600,
                None,
                manager_uptime_seconds=1800.0,
                run_timed_out=True,
            )

        assert any(
            "decision=counted-run-timed-out" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )
        ctx.gtd.relaunch_manage_rollout.assert_called_once()

    async def test_watchdog_idempotency_stamp(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A free relaunch stamps _watchdog_acted so a concurrent tick doesn't double-act."""
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch import main as main_module

        run = _make_run()
        engine = MagicMock()
        rollout = _rollout(
            "running",
            manager_phase="polling",
            manager_state_updated_at=(
                datetime.now(UTC) - timedelta(seconds=60)
            ).isoformat(),
            in_flight=[{"runId": "run-1", "itemId": "item-1", "status": "running"}],
        )

        with _full_patch_stack() as ctx:
            ctx.gtd.get_rollout = AsyncMock(return_value=rollout)
            ctx.gtd.relaunch_manage_rollout = AsyncMock()
            ctx.gtd.halt_rollout = AsyncMock()
            ctx.gtd.post_comment = AsyncMock()
            ctx.db.insert_run = AsyncMock()

            await _maybe_relaunch_manage(
                run,
                100,
                engine,
                3600,
                None,
                manager_uptime_seconds=1800.0,
                run_timed_out=False,
            )

            assert run.rollout_id in ctx.watchdog_acted

            stale_watchdog_rollout = {
                "id": run.rollout_id,
                "status": "running",
                "manager_phase": "polling",
                "manager_state_updated_at": (
                    datetime.now(UTC)
                    - timedelta(seconds=config.MANAGE_STALE_THRESHOLD_SECONDS * 2)
                ).isoformat(),
                "manage_retry_count": 0,
                "project_id": "proj-1",
                "inFlightBuildRuns": [],
            }
            ctx.gtd.list_running_rollouts = AsyncMock(
                return_value=[stale_watchdog_rollout]
            )

            with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
                await main_module._watchdog_tick()

            ctx.gtd.relaunch_manage_rollout.assert_not_called()
            assert any(
                "decision=skipped-idempotency" in r.getMessage() for r in caplog.records
            )

    async def test_counted_recovery_clears_tally(self) -> None:
        """A counted recovery clears the free-relaunch tally at the shared choke point."""
        from agent_gtd_dispatch.main import _do_manage_recovery

        run = _make_run()
        engine = MagicMock()

        with _full_patch_stack() as ctx:
            ctx.free_relaunches["rollout-abc"] = 4
            ctx.gtd.relaunch_manage_rollout = AsyncMock(
                return_value=_rollout("running", retry_count=1)
            )
            ctx.gtd.halt_rollout = AsyncMock()
            ctx.db.insert_run = AsyncMock()

            await _do_manage_recovery(
                "rollout-abc",
                run,
                100,
                engine,
                3600,
                None,
                halt_reason="manage_relaunch_cap_exceeded",
            )

            assert "rollout-abc" not in ctx.free_relaunches

    async def test_all_eight_decisions_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Each of the eight ladder decisions is logged at its expected level."""
        from agent_gtd_dispatch import config

        now_minus_60 = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        in_flight_one = [{"runId": "run-1", "itemId": "item-1", "status": "running"}]

        scenarios: list[tuple[str, int, dict, dict]] = [
            (
                "counted-run-timed-out",
                logging.WARNING,
                _rollout(
                    "running",
                    manager_phase="polling",
                    manager_state_updated_at=now_minus_60,
                    in_flight=in_flight_one,
                ),
                {"manager_uptime_seconds": 1800.0, "run_timed_out": True},
            ),
            (
                "counted-not-polling",
                logging.INFO,
                _rollout("running"),
                {"manager_uptime_seconds": 1800.0, "run_timed_out": False},
            ),
            (
                "counted-no-build-in-flight",
                logging.INFO,
                _rollout(
                    "running",
                    manager_phase="polling",
                    manager_state_updated_at=now_minus_60,
                    in_flight=[],
                ),
                {"manager_uptime_seconds": 1800.0, "run_timed_out": False},
            ),
            (
                "counted-short-uptime",
                logging.INFO,
                _rollout(
                    "running",
                    manager_phase="polling",
                    manager_state_updated_at=now_minus_60,
                    in_flight=in_flight_one,
                ),
                {"manager_uptime_seconds": 5.0, "run_timed_out": False},
            ),
            (
                "counted-unknown-manager-state-age",
                logging.INFO,
                _rollout("running", manager_phase="polling", in_flight=in_flight_one),
                {"manager_uptime_seconds": 1800.0, "run_timed_out": False},
            ),
            (
                "counted-backstop-exceeded",
                logging.WARNING,
                _rollout(
                    "running",
                    manager_phase="polling",
                    manager_state_updated_at=(
                        datetime.now(UTC)
                        - timedelta(seconds=config.MANAGE_TIMEOUT_SECONDS + 600)
                    ).isoformat(),
                    in_flight=in_flight_one,
                ),
                {"manager_uptime_seconds": 1800.0, "run_timed_out": False},
            ),
            (
                "counted-free-cap-exhausted",
                logging.WARNING,
                _rollout(
                    "running",
                    manager_phase="polling",
                    manager_state_updated_at=now_minus_60,
                    in_flight=in_flight_one,
                ),
                {"manager_uptime_seconds": 1800.0, "run_timed_out": False},
            ),
            (
                "free-relaunch-build-in-flight",
                logging.INFO,
                _rollout(
                    "running",
                    manager_phase="polling",
                    manager_state_updated_at=now_minus_60,
                    in_flight=in_flight_one,
                ),
                {"manager_uptime_seconds": 1800.0, "run_timed_out": False},
            ),
        ]

        run = _make_run()
        engine = MagicMock()

        with (
            _full_patch_stack() as ctx,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            ctx.gtd.relaunch_manage_rollout = AsyncMock(
                return_value=_rollout("running", retry_count=1)
            )
            ctx.gtd.halt_rollout = AsyncMock()
            ctx.gtd.post_comment = AsyncMock()
            ctx.db.insert_run = AsyncMock()

            for decision, level, rollout, kwargs in scenarios:
                if decision == "counted-free-cap-exhausted":
                    ctx.free_relaunches[run.rollout_id] = (
                        config.MAX_MANAGE_FREE_RELAUNCHES
                    )
                else:
                    ctx.free_relaunches.pop(run.rollout_id, None)
                ctx.gtd.get_rollout = AsyncMock(return_value=rollout)
                caplog.clear()
                await _maybe_relaunch_manage(run, 100, engine, 3600, None, **kwargs)
                messages_at_level = [
                    r.getMessage() for r in caplog.records if r.levelno == level
                ]
                assert any(f"decision={decision}" in m for m in messages_at_level), (
                    f"expected decision={decision} at level {level}, got: "
                    f"{[r.getMessage() for r in caplog.records]}"
                )

    async def test_progress_reset_on_item_completion(self) -> None:
        """Completing item 1 resets the tally so item 2 starts with a fresh budget."""
        from agent_gtd_dispatch import config

        run = _make_run()
        engine = MagicMock()

        def _waiting_on(item_id: str, run_id: str) -> dict:
            return _rollout(
                "running",
                manager_phase="polling",
                manager_state_updated_at=(
                    datetime.now(UTC) - timedelta(seconds=60)
                ).isoformat(),
                in_flight=[{"runId": run_id, "itemId": item_id, "status": "running"}],
            )

        with _full_patch_stack() as ctx:
            ctx.gtd.relaunch_manage_rollout = AsyncMock()
            ctx.gtd.halt_rollout = AsyncMock()
            ctx.gtd.post_comment = AsyncMock()
            ctx.db.insert_run = AsyncMock()

            # MAX_MANAGE_FREE_RELAUNCHES - 1 free relaunches while waiting on item 1.
            for _ in range(config.MAX_MANAGE_FREE_RELAUNCHES - 1):
                ctx.gtd.get_rollout = AsyncMock(
                    return_value=_waiting_on("item-1", "run-1")
                )
                await _maybe_relaunch_manage(
                    run,
                    100,
                    engine,
                    3600,
                    None,
                    manager_uptime_seconds=1800.0,
                    run_timed_out=False,
                )

            assert (
                ctx.free_relaunches[run.rollout_id]
                == config.MAX_MANAGE_FREE_RELAUNCHES - 1
            )
            ctx.gtd.halt_rollout.assert_not_called()

            # item-1 completes; the manager moves on to item-2.
            ctx.gtd.get_rollout = AsyncMock(return_value=_waiting_on("item-2", "run-2"))
            await _maybe_relaunch_manage(
                run,
                100,
                engine,
                3600,
                None,
                manager_uptime_seconds=1800.0,
                run_timed_out=False,
            )

            # The tally reset on item-1's completion, so item-2 starts fresh.
            assert ctx.free_relaunches[run.rollout_id] == 1
            ctx.gtd.halt_rollout.assert_not_called()

            # Waiting on item-2 must not inherit item-1's exhausted budget.
            for _ in range(config.MAX_MANAGE_FREE_RELAUNCHES - 2):
                ctx.gtd.get_rollout = AsyncMock(
                    return_value=_waiting_on("item-2", "run-2")
                )
                await _maybe_relaunch_manage(
                    run,
                    100,
                    engine,
                    3600,
                    None,
                    manager_uptime_seconds=1800.0,
                    run_timed_out=False,
                )

            ctx.gtd.halt_rollout.assert_not_called()
            assert (
                ctx.free_relaunches[run.rollout_id]
                == config.MAX_MANAGE_FREE_RELAUNCHES - 1
            )

    async def test_worker_wiring_never_launched_zero_uptime(self) -> None:
        """A manage run that fails before dispatch.run_agent reports 0.0 uptime."""
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import CLAUDE
        from agent_gtd_dispatch.main import _dispatch_worker
        from agent_gtd_dispatch.models import Run

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
            patch(
                "agent_gtd_dispatch.main._maybe_relaunch_manage", new=AsyncMock()
            ) as mock_relaunch,
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
            mock_dispatch.prepare_manage_workspace.side_effect = Exception("boom")
            mock_dispatch.cleanup_workspace = MagicMock()

            await _dispatch_worker(run, 100, CLAUDE, 3600)

        mock_relaunch.assert_awaited_once()
        _, kwargs = mock_relaunch.call_args
        assert kwargs["run_timed_out"] is False
        assert kwargs["manager_uptime_seconds"] == 0.0

    async def test_worker_wiring_timeout_sets_run_timed_out(self, tmp_path) -> None:
        """A manage run whose agent subprocess times out reports run_timed_out=True."""
        import subprocess

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import CLAUDE
        from agent_gtd_dispatch.main import _dispatch_worker
        from agent_gtd_dispatch.models import Run

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
            patch(
                "agent_gtd_dispatch.main._maybe_relaunch_manage", new=AsyncMock()
            ) as mock_relaunch,
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
            mock_dispatch.prepare_manage_workspace.return_value = tmp_path
            mock_dispatch.build_system_prompt.return_value = "prompt"
            mock_dispatch.run_agent = AsyncMock(
                side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=3600)
            )
            mock_dispatch.cleanup_workspace = MagicMock()

            await _dispatch_worker(run, 100, CLAUDE, 3600)

        mock_relaunch.assert_awaited_once()
        _, kwargs = mock_relaunch.call_args
        assert kwargs["run_timed_out"] is True
