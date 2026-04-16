"""Tests for db.py — SQLite persistence layer."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from agent_gtd_dispatch import config, db
from agent_gtd_dispatch.models import Run, RunStatus


@pytest.fixture(autouse=True)
def _env(tmp_path):
    """Set required env vars and use tmp path for workspace/db."""
    env = {
        "DISPATCH_API_KEY": "test-key",
        "AGENT_GTD_URL": "http://localhost:9999",
        "AGENT_GTD_API_KEY": "test-gtd-key",
        "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
    }
    with patch.dict(os.environ, env):
        config.load()
        yield


class TestReconcileOrphans:
    async def test_returns_zero_when_no_runs(self) -> None:
        await db.init_db()
        count = await db.reconcile_orphans()
        assert count == 0

    async def test_pending_run_marked_failed(self) -> None:
        await db.init_db()
        run = Run(item_id="item1", project_name="proj", branch_name="feat/x")
        assert run.status == RunStatus.pending
        await db.insert_run(run)

        count = await db.reconcile_orphans()

        assert count == 1
        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status == RunStatus.failed
        assert updated.error == "Service restarted while run was active"

    async def test_running_run_marked_failed(self) -> None:
        await db.init_db()
        run = Run(item_id="item1", project_name="proj", branch_name="feat/x")
        await db.insert_run(run)
        await db.update_run(run.id, status=RunStatus.running)

        count = await db.reconcile_orphans()

        assert count == 1
        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status == RunStatus.failed
        assert updated.error == "Service restarted while run was active"

    async def test_terminal_runs_not_touched(self) -> None:
        await db.init_db()
        terminal_statuses = [
            RunStatus.succeeded,
            RunStatus.failed,
            RunStatus.cancelled,
            RunStatus.timed_out,
        ]
        for status in terminal_statuses:
            run = Run(item_id="item1", project_name="proj", branch_name="feat/x")
            await db.insert_run(run)
            await db.update_run(run.id, status=status)

        count = await db.reconcile_orphans()

        assert count == 0

    async def test_multiple_orphans_all_reconciled(self) -> None:
        await db.init_db()
        runs = [
            Run(item_id=f"item{i}", project_name="proj", branch_name=f"feat/x{i}")
            for i in range(3)
        ]
        for run in runs:
            await db.insert_run(run)
        # Manually set one to running
        await db.update_run(runs[0].id, status=RunStatus.running)

        count = await db.reconcile_orphans()

        assert count == 3
        for run in runs:
            updated = await db.get_run(run.id)
            assert updated is not None
            assert updated.status == RunStatus.failed
