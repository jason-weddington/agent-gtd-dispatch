"""Tests for the cancel endpoint — cross-service cancel propagation."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(tmp_path):
    """Set required env vars and use tmp path for workspace/db."""
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


@pytest.fixture
def client():
    from agent_gtd_dispatch.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-key"}


def _make_run(
    *,
    status: str = "running",
    item_id: str | None = "item-abc",
    mode: str = "build",
):
    """Build and return a Run model (not inserted into DB)."""
    from agent_gtd_dispatch.models import Run, RunStatus

    return Run(
        item_id=item_id,
        project_name="test-project",
        branch_name="feat/item-abc-fix",
        mode=mode,
        engine="claude-code",
        status=RunStatus(status),
        created_at=datetime.now(UTC),
    )


class TestCancelRun:
    # ------------------------------------------------------------------
    # AC-6: unknown run_id → 404
    # ------------------------------------------------------------------

    def test_cancel_unknown_run_returns_404(self, client, auth_headers):
        resp = client.post("/runs/nonexistent-run-id/cancel", headers=auth_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Run not found"

    # ------------------------------------------------------------------
    # AC-5: terminal states → idempotent 200, no side effects
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "terminal_status",
        ["succeeded", "failed", "timed_out", "cancelled"],
    )
    async def test_cancel_terminal_run_returns_200_no_side_effects(
        self, client, auth_headers, terminal_status
    ) -> None:
        from agent_gtd_dispatch import db

        await db.init_db()
        run = _make_run(status=terminal_status)
        await db.insert_run(run)

        with patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd:
            mock_gtd.post_comment = AsyncMock()

            resp = client.post(f"/runs/{run.id}/cancel", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == terminal_status
        # No comment should have been posted
        mock_gtd.post_comment.assert_not_called()

    # ------------------------------------------------------------------
    # AC-1 + AC-2: active run — SIGTERM, wait grace, SIGKILL, DB updated
    # ------------------------------------------------------------------

    async def test_cancel_sends_sigterm_then_sigkill_when_process_lingers(
        self, client, auth_headers
    ) -> None:
        from agent_gtd_dispatch import config, db
        from agent_gtd_dispatch.main import (
            _active_processes,
            _active_subprocesses,
            _run_event_queues,
        )
        from agent_gtd_dispatch.models import RunStatus

        await db.init_db()
        run = _make_run(status="running")
        await db.insert_run(run)

        # Mock subprocess: poll() returns None (still alive) so SIGKILL fires
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # process still alive after grace period
        _active_subprocesses[run.id] = mock_proc

        # Mock asyncio task
        mock_task = MagicMock(spec=asyncio.Task)
        _active_processes[run.id] = mock_task

        # Set up event queue
        _run_event_queues[run.id] = asyncio.Queue()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch(
                "agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()
            ) as mock_sleep,
        ):
            mock_gtd.post_comment = AsyncMock()

            resp = client.post(f"/runs/{run.id}/cancel", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cancelled"
        assert data["completed_at"] is not None

        # SIGTERM sent
        mock_proc.terminate.assert_called_once()
        # Grace period waited
        mock_sleep.assert_called_once_with(config.CANCEL_GRACE_SECONDS)
        # SIGKILL sent (process was still alive)
        mock_proc.kill.assert_called_once()

        # Task was cancelled
        mock_task.cancel.assert_called_once()

        # DB updated to cancelled
        final = await db.get_run(run.id)
        assert final is not None
        assert final.status == RunStatus.cancelled
        assert final.completed_at is not None

    # ------------------------------------------------------------------
    # AC-2: SIGKILL NOT sent when process exits during grace period
    # ------------------------------------------------------------------

    async def test_cancel_no_sigkill_when_process_exits_during_grace(
        self, client, auth_headers
    ) -> None:
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.main import (
            _active_processes,
            _active_subprocesses,
            _run_event_queues,
        )

        await db.init_db()
        run = _make_run(status="running")
        await db.insert_run(run)

        # Mock subprocess: poll() returns 0 (exited during grace period)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        _active_subprocesses[run.id] = mock_proc
        _active_processes[run.id] = MagicMock(spec=asyncio.Task)
        _run_event_queues[run.id] = asyncio.Queue()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()),
        ):
            mock_gtd.post_comment = AsyncMock()
            resp = client.post(f"/runs/{run.id}/cancel", headers=auth_headers)

        assert resp.status_code == 200
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_not_called()  # no SIGKILL needed

    # ------------------------------------------------------------------
    # AC-4: comment posted to item_id
    # ------------------------------------------------------------------

    async def test_cancel_posts_comment_to_item_id(self, client, auth_headers) -> None:
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.main import _run_event_queues

        await db.init_db()
        run = _make_run(status="running", item_id="item-xyz")
        await db.insert_run(run)
        _run_event_queues[run.id] = asyncio.Queue()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()),
        ):
            mock_gtd.post_comment = AsyncMock()

            resp = client.post(f"/runs/{run.id}/cancel", headers=auth_headers)

        assert resp.status_code == 200
        # token=None: the test Run has no callback_token, so it falls back to
        # config.AGENT_GTD_API_KEY inside gtd_client._request.
        mock_gtd.post_comment.assert_called_once_with(
            "item-xyz",
            "Run cancelled by lead via agent-gtd",
            created_by="agent-gtd-dispatch",
            token=None,
        )

    # ------------------------------------------------------------------
    # AC-4: comment NOT posted when item_id is None (manage-mode run)
    # ------------------------------------------------------------------

    async def test_cancel_no_comment_when_item_id_is_none(
        self, client, auth_headers
    ) -> None:
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.main import _run_event_queues

        await db.init_db()
        run = _make_run(status="running", item_id=None, mode="manage")
        await db.insert_run(run)
        _run_event_queues[run.id] = asyncio.Queue()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()),
        ):
            mock_gtd.post_comment = AsyncMock()

            resp = client.post(f"/runs/{run.id}/cancel", headers=auth_headers)

        assert resp.status_code == 200
        mock_gtd.post_comment.assert_not_called()

    # ------------------------------------------------------------------
    # AC-3 + AC-7e: SSE event is published to the run's event queue
    # ------------------------------------------------------------------

    async def test_cancel_publishes_sse_event(self, client, auth_headers) -> None:
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.main import _run_event_queues

        await db.init_db()
        run = _make_run(status="running")
        await db.insert_run(run)

        queue: asyncio.Queue[dict] = asyncio.Queue()
        _run_event_queues[run.id] = queue

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()),
        ):
            mock_gtd.post_comment = AsyncMock()

            resp = client.post(f"/runs/{run.id}/cancel", headers=auth_headers)

        assert resp.status_code == 200

        # SSE event should be in the queue
        assert not queue.empty()
        event = queue.get_nowait()
        assert event["event"] == "run-status-change"
        assert event["run_id"] == run.id
        assert event["status"] == "cancelled"
        assert event["completed_at"] is not None

    # ------------------------------------------------------------------
    # AC-4: comment post failure is silently logged, not raised
    # ------------------------------------------------------------------

    async def test_cancel_comment_failure_does_not_raise(
        self, client, auth_headers
    ) -> None:
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.main import _run_event_queues
        from agent_gtd_dispatch.models import RunStatus

        await db.init_db()
        run = _make_run(status="running", item_id="item-fail-comment")
        await db.insert_run(run)
        _run_event_queues[run.id] = asyncio.Queue()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()),
        ):
            mock_gtd.post_comment = AsyncMock(side_effect=Exception("network error"))

            resp = client.post(f"/runs/{run.id}/cancel", headers=auth_headers)

        # Should still succeed even though comment post failed
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

        # DB should still be updated
        final = await db.get_run(run.id)
        assert final is not None
        assert final.status == RunStatus.cancelled

    # ------------------------------------------------------------------
    # AC-5: pending run (no subprocess registered) — idempotent-style cancel
    # ------------------------------------------------------------------

    async def test_cancel_pending_run_no_subprocess(self, client, auth_headers) -> None:
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.main import _run_event_queues
        from agent_gtd_dispatch.models import RunStatus

        await db.init_db()
        run = _make_run(status="pending")
        await db.insert_run(run)
        _run_event_queues[run.id] = asyncio.Queue()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_gtd,
            patch("agent_gtd_dispatch.main.asyncio.sleep", new=AsyncMock()),
        ):
            mock_gtd.post_comment = AsyncMock()

            resp = client.post(f"/runs/{run.id}/cancel", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cancelled"

        final = await db.get_run(run.id)
        assert final is not None
        assert final.status == RunStatus.cancelled
