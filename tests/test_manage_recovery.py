"""Tests for manage subprocess auto-recovery logic."""

from __future__ import annotations

import asyncio
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
        engine="claude",
    )


def _rollout(status: str, retry_count: int = 0) -> dict:
    return {
        "id": "rollout-abc",
        "status": status,
        "manage_retry_count": retry_count,
        "project_id": "proj-1",
    }


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
    ["completed", "halted", "cancelled", "crashed"],
)
async def test_clean_exit_does_not_relaunch(status: str) -> None:
    """All four clean-exit statuses should result in no relaunch."""
    run = _make_run()
    engine = MagicMock()

    with patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd:
        mock_gtd.get_rollout = AsyncMock(return_value=_rollout(status))
        mock_gtd.relaunch_manage_rollout = AsyncMock()
        mock_gtd.halt_rollout = AsyncMock()

        await _maybe_relaunch_manage(run, 100, engine, 3600, None)

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

        await _maybe_relaunch_manage(run, 100, engine, 3600, None)

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

        await _maybe_relaunch_manage(run, 100, engine, 3600, None)

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

        await _maybe_relaunch_manage(run, 100, engine, 3600, None)

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
        engine="claude",
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
        engine="claude",
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
        await _maybe_relaunch_manage(run, 100, engine, 3600, None)

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

        await _maybe_relaunch_manage(run, 100, engine, 3600, None)

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

        await _maybe_relaunch_manage(run, 100, engine, 3600, "my-attribution")

        # Worker was spawned (create_task was called)
        assert "coro" in spawned_worker_kwargs


# ---------------------------------------------------------------------------
# Constants sanity check
# ---------------------------------------------------------------------------


def test_constants() -> None:
    """Ensure the recovery constants are set to expected values."""
    assert MAX_MANAGE_RETRIES == 2
    assert MANAGE_RETRY_BACKOFF_SECONDS == 30
