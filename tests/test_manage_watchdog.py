"""Tests for the manage-agent staleness watchdog (AC-1 through AC-7)."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(tmp_path):
    """Set required env vars and reload config — mirrors the pattern in test_api.py."""
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


def _stale_rollout(
    rollout_id: str = "rollout-stale",
    status: str = "running",
    seconds_ago: int | None = None,
) -> dict:
    """Build a rollout dict whose manager_state_updated_at is stale.

    Default age is twice the configured staleness threshold so the helper
    stays correct if MANAGE_STALE_THRESHOLD_SECONDS is retuned.
    """
    if seconds_ago is None:
        from agent_gtd_dispatch import config

        seconds_ago = config.MANAGE_STALE_THRESHOLD_SECONDS * 2
    old_time = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()
    return {
        "id": rollout_id,
        "status": status,
        "manager_phase": "polling",
        "manager_state_updated_at": old_time,
        "manage_retry_count": 0,
        "project_id": "proj-1",
    }


def _fresh_rollout(rollout_id: str = "rollout-fresh") -> dict:
    """Build a rollout dict whose manager_state_updated_at is recent."""
    fresh_time = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    return {
        "id": rollout_id,
        "status": "running",
        "manager_phase": "polling",
        "manager_state_updated_at": fresh_time,
        "manage_retry_count": 0,
        "project_id": "proj-1",
    }


def _make_manage_run(rollout_id: str = "rollout-stale"):
    """Build a minimal manage-mode Run."""
    from agent_gtd_dispatch.models import Run

    return Run(
        item_id=None,
        project_name="test-project",
        mode="manage",
        rollout_id=rollout_id,
        engine="claude-code",
    )


# ---------------------------------------------------------------------------
# AC-1: Lifespan creates and cancels the watchdog task
# ---------------------------------------------------------------------------


class TestAC1Lifespan:
    async def test_watchdog_task_created_on_startup_and_cancelled_on_shutdown(
        self,
    ) -> None:
        """AC-1: lifespan creates _watchdog_task; it is done after shutdown."""
        from fastapi.testclient import TestClient

        from agent_gtd_dispatch import main

        # Reset so any previous test run doesn't bleed through
        main._watchdog_task = None

        captured_task = None
        with TestClient(main.app):
            captured_task = main._watchdog_task
            assert captured_task is not None, "Watchdog task was not created on startup"
            assert not captured_task.done(), (
                "Watchdog task should be running during startup"
            )

        # After TestClient exits, lifespan shutdown cancels the task
        assert captured_task.done(), "Watchdog task should be done after shutdown"


# ---------------------------------------------------------------------------
# AC-2: Stale rollout with live subprocess triggers recovery
# ---------------------------------------------------------------------------


class TestAC2StaleDetection:
    async def test_stale_polling_rollout_triggers_recovery(self) -> None:
        """AC-2: stale rollout with live subprocess → recovery invoked exactly once."""
        from agent_gtd_dispatch import main

        stale = _stale_rollout()
        fake_run = _make_manage_run(stale["id"])
        fake_task = MagicMock(spec=asyncio.Task)

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch(
                "agent_gtd_dispatch.main._active_processes", {fake_run.id: fake_task}
            ),
            patch("agent_gtd_dispatch.main._rollout_to_run", {stale["id"]: fake_run}),
            patch("agent_gtd_dispatch.main._watchdog_acted", {}),
            patch("agent_gtd_dispatch.main.db") as mock_db,
            patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()),
            patch("agent_gtd_dispatch.main.asyncio.create_task") as mock_create_task,
        ):
            mock_gtd.list_running_rollouts = AsyncMock(return_value=[stale])
            mock_gtd.relaunch_manage_rollout = AsyncMock(
                return_value={**stale, "manage_retry_count": 1}
            )
            mock_db.insert_run = AsyncMock()
            mock_create_task.return_value = MagicMock()

            await main._watchdog_tick()

            # Recovery was triggered
            mock_gtd.relaunch_manage_rollout.assert_called_once_with(stale["id"])
            # New worker spawned
            mock_create_task.assert_called_once()


# ---------------------------------------------------------------------------
# AC-3: Relaunch branch and halt branch
# ---------------------------------------------------------------------------


class TestAC3RecoveryBranches:
    async def test_relaunch_when_under_retry_cap(self) -> None:
        """AC-3a: retry_count <= MAX_MANAGE_RETRIES → new _dispatch_worker spawned."""
        from agent_gtd_dispatch import main

        stale = _stale_rollout()
        fake_run = _make_manage_run(stale["id"])

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main._active_processes", {}),
            patch("agent_gtd_dispatch.main._rollout_to_run", {stale["id"]: fake_run}),
            patch("agent_gtd_dispatch.main._watchdog_acted", {}),
            patch("agent_gtd_dispatch.main.db") as mock_db,
            patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()),
            patch("agent_gtd_dispatch.main.asyncio.create_task") as mock_create_task,
        ):
            mock_gtd.list_running_rollouts = AsyncMock(return_value=[stale])
            mock_gtd.relaunch_manage_rollout = AsyncMock(
                return_value={**stale, "manage_retry_count": 1}
            )
            mock_gtd.halt_rollout = AsyncMock()
            mock_db.insert_run = AsyncMock()
            mock_create_task.return_value = MagicMock()

            await main._watchdog_tick()

            mock_create_task.assert_called_once()
            mock_gtd.halt_rollout.assert_not_called()

    async def test_halt_when_retry_cap_exceeded(self) -> None:
        """AC-3b: retry_count > MAX_MANAGE_RETRIES → halt_rollout with 'manage_watchdog_stale'."""
        from agent_gtd_dispatch import main
        from agent_gtd_dispatch.main import MAX_MANAGE_RETRIES

        stale = _stale_rollout()
        fake_run = _make_manage_run(stale["id"])
        exceeded = MAX_MANAGE_RETRIES + 1

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main._active_processes", {}),
            patch("agent_gtd_dispatch.main._rollout_to_run", {stale["id"]: fake_run}),
            patch("agent_gtd_dispatch.main._watchdog_acted", {}),
            patch("agent_gtd_dispatch.main.db") as mock_db,
            patch("agent_gtd_dispatch.main.asyncio.create_task") as mock_create_task,
        ):
            mock_gtd.list_running_rollouts = AsyncMock(return_value=[stale])
            mock_gtd.relaunch_manage_rollout = AsyncMock(
                return_value={**stale, "manage_retry_count": exceeded}
            )
            mock_gtd.halt_rollout = AsyncMock()
            mock_db.insert_run = AsyncMock()
            mock_create_task.return_value = MagicMock()

            await main._watchdog_tick()

            mock_gtd.halt_rollout.assert_called_once_with(
                stale["id"], reason="manage_watchdog_stale"
            )
            mock_create_task.assert_not_called()

    async def test_live_subprocess_is_killed_before_relaunch(self) -> None:
        """AC-3: existing subprocess is terminated and asyncio task cancelled."""
        from agent_gtd_dispatch import main

        stale = _stale_rollout()
        fake_run = _make_manage_run(stale["id"])
        fake_task = MagicMock(spec=asyncio.Task)
        fake_proc = MagicMock()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch(
                "agent_gtd_dispatch.main._active_processes",
                {fake_run.id: fake_task},
            ),
            patch(
                "agent_gtd_dispatch.main._active_subprocesses",
                {fake_run.id: fake_proc},
            ),
            patch("agent_gtd_dispatch.main._rollout_to_run", {stale["id"]: fake_run}),
            patch("agent_gtd_dispatch.main._watchdog_acted", {}),
            patch("agent_gtd_dispatch.main.db") as mock_db,
            patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()),
            patch("agent_gtd_dispatch.main.asyncio.create_task") as mock_create_task,
        ):
            mock_gtd.list_running_rollouts = AsyncMock(return_value=[stale])
            mock_gtd.relaunch_manage_rollout = AsyncMock(
                return_value={**stale, "manage_retry_count": 1}
            )
            mock_db.insert_run = AsyncMock()
            mock_create_task.return_value = MagicMock()

            await main._watchdog_tick()

            fake_task.cancel.assert_called_once()
            fake_proc.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# AC-4: Fresh and terminal rollouts are not touched
# ---------------------------------------------------------------------------


class TestAC4NoOpCases:
    async def test_fresh_timestamp_not_touched(self) -> None:
        """AC-4a: rollout with fresh manager_state_updated_at → zero relaunch/halt calls."""
        from agent_gtd_dispatch import main

        fresh = _fresh_rollout()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main._watchdog_acted", {}),
        ):
            mock_gtd.list_running_rollouts = AsyncMock(return_value=[fresh])
            mock_gtd.relaunch_manage_rollout = AsyncMock()
            mock_gtd.halt_rollout = AsyncMock()

            await main._watchdog_tick()

            mock_gtd.relaunch_manage_rollout.assert_not_called()
            mock_gtd.halt_rollout.assert_not_called()

    async def test_terminal_status_not_touched(self) -> None:
        """AC-4b: rollout with terminal status → zero relaunch/halt calls."""
        from agent_gtd_dispatch import main
        from agent_gtd_dispatch.main import _CLEAN_EXIT_STATUSES

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main._watchdog_acted", {}),
        ):
            mock_gtd.relaunch_manage_rollout = AsyncMock()
            mock_gtd.halt_rollout = AsyncMock()

            for status in _CLEAN_EXIT_STATUSES:
                terminal = _stale_rollout(rollout_id=f"rollout-{status}", status=status)
                mock_gtd.list_running_rollouts = AsyncMock(return_value=[terminal])
                await main._watchdog_tick()

            mock_gtd.relaunch_manage_rollout.assert_not_called()
            mock_gtd.halt_rollout.assert_not_called()

    async def test_both_fresh_and_terminal_in_batch(self) -> None:
        """AC-4: batch with fresh + terminal → both skipped; zero calls."""
        from agent_gtd_dispatch import main

        fresh = _fresh_rollout(rollout_id="rollout-fresh")
        terminal = _stale_rollout(rollout_id="rollout-completed", status="completed")

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main._watchdog_acted", {}),
        ):
            mock_gtd.list_running_rollouts = AsyncMock(return_value=[fresh, terminal])
            mock_gtd.relaunch_manage_rollout = AsyncMock()
            mock_gtd.halt_rollout = AsyncMock()

            await main._watchdog_tick()

            mock_gtd.relaunch_manage_rollout.assert_not_called()
            mock_gtd.halt_rollout.assert_not_called()


# ---------------------------------------------------------------------------
# AC-5: Idempotency across ticks
# ---------------------------------------------------------------------------


class TestAC5Idempotency:
    async def test_two_consecutive_ticks_produce_exactly_one_relaunch(
        self,
    ) -> None:
        """AC-5: second tick within staleness window does NOT re-relaunch."""
        from agent_gtd_dispatch import main

        stale = _stale_rollout()
        shared_acted: dict = {}

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main._active_processes", {}),
            patch("agent_gtd_dispatch.main._rollout_to_run", {}),
            patch("agent_gtd_dispatch.main._watchdog_acted", shared_acted),
            patch("agent_gtd_dispatch.main.db") as mock_db,
            patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()),
            patch("agent_gtd_dispatch.main.asyncio.create_task") as mock_create_task,
        ):
            mock_gtd.list_running_rollouts = AsyncMock(return_value=[stale])
            mock_gtd.relaunch_manage_rollout = AsyncMock(
                return_value={**stale, "manage_retry_count": 1}
            )
            mock_db.insert_run = AsyncMock()
            mock_create_task.return_value = MagicMock()

            # First tick
            await main._watchdog_tick()
            assert mock_gtd.relaunch_manage_rollout.call_count == 1

            # Second tick — idempotency guard should block re-relaunch
            await main._watchdog_tick()
            assert mock_gtd.relaunch_manage_rollout.call_count == 1


# ---------------------------------------------------------------------------
# AC-6: Config defaults
# ---------------------------------------------------------------------------


class TestAC6ConfigDefaults:
    def test_stale_threshold_default_present(self) -> None:
        """AC-6: MANAGE_STALE_THRESHOLD_SECONDS has a sane default."""
        from agent_gtd_dispatch import config

        assert config.MANAGE_STALE_THRESHOLD_SECONDS > 0

    def test_watchdog_interval_default_present(self) -> None:
        """AC-6: WATCHDOG_INTERVAL_SECONDS has a sane default."""
        from agent_gtd_dispatch import config

        assert config.WATCHDOG_INTERVAL_SECONDS > 0

    def test_stale_threshold_less_than_manage_timeout(self) -> None:
        """AC-6: threshold must be well under MANAGE_TIMEOUT_SECONDS (4h)."""
        from agent_gtd_dispatch import config

        assert config.MANAGE_STALE_THRESHOLD_SECONDS < config.MANAGE_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# AC-7: Per-rollout error isolation
# ---------------------------------------------------------------------------


class TestAC7ErrorIsolation:
    async def test_exception_in_one_rollout_does_not_stop_others(self) -> None:
        """AC-7: bad rollout raises → watchdog logs, continues to next rollout."""
        from agent_gtd_dispatch import main

        bad_rollout = _stale_rollout(rollout_id="rollout-bad")
        good_rollout = _stale_rollout(rollout_id="rollout-good")
        shared_acted: dict = {}

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main._active_processes", {}),
            patch("agent_gtd_dispatch.main._rollout_to_run", {}),
            patch("agent_gtd_dispatch.main._watchdog_acted", shared_acted),
            patch("agent_gtd_dispatch.main.db") as mock_db,
            patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()),
            patch("agent_gtd_dispatch.main.asyncio.create_task") as mock_create_task,
        ):
            mock_gtd.list_running_rollouts = AsyncMock(
                return_value=[bad_rollout, good_rollout]
            )

            call_count = 0

            async def relaunch_side_effect(rollout_id):
                nonlocal call_count
                call_count += 1
                if rollout_id == bad_rollout["id"]:
                    raise RuntimeError("simulated upstream failure")
                return {**good_rollout, "manage_retry_count": 1}

            mock_gtd.relaunch_manage_rollout = AsyncMock(
                side_effect=relaunch_side_effect
            )
            mock_db.insert_run = AsyncMock()
            mock_create_task.return_value = MagicMock()

            # Must not raise
            await main._watchdog_tick()

            # Both rollouts were attempted (relaunch called twice)
            assert call_count == 2
            # Good rollout still spawned a new worker
            mock_create_task.assert_called_once()

    async def test_list_rollouts_failure_does_not_crash_watchdog(self) -> None:
        """AC-7: gtd_client.list_running_rollouts raising → watchdog logs and returns."""
        from agent_gtd_dispatch import main

        with patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd:
            mock_gtd.list_running_rollouts = AsyncMock(
                side_effect=RuntimeError("network error")
            )
            mock_gtd.relaunch_manage_rollout = AsyncMock()

            # Must not raise
            await main._watchdog_tick()

            mock_gtd.relaunch_manage_rollout.assert_not_called()
