"""Tests for db.py — SQLite persistence layer."""

from __future__ import annotations

import json
import os
import sqlite3
from unittest.mock import patch

import pytest

from agent_gtd_dispatch import config, db
from agent_gtd_dispatch.models import PushStatus, RepoPushStatus, Run, RunStatus


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
        config.load()
        yield


class TestReconcileOrphans:
    async def test_returns_empty_list_when_no_runs(self) -> None:
        await db.init_db()
        orphaned_ids = await db.reconcile_orphans()
        assert orphaned_ids == []

    async def test_pending_run_marked_failed(self) -> None:
        await db.init_db()
        run = Run(item_id="item1", project_name="proj", branch_name="feat/x")
        assert run.status == RunStatus.pending
        await db.insert_run(run)

        orphaned_ids = await db.reconcile_orphans()

        assert len(orphaned_ids) == 1
        assert run.id in orphaned_ids
        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.status == RunStatus.failed
        assert updated.error == "Service restarted while run was active"

    async def test_running_run_marked_failed(self) -> None:
        await db.init_db()
        run = Run(item_id="item1", project_name="proj", branch_name="feat/x")
        await db.insert_run(run)
        await db.update_run(run.id, status=RunStatus.running)

        orphaned_ids = await db.reconcile_orphans()

        assert len(orphaned_ids) == 1
        assert run.id in orphaned_ids
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

        orphaned_ids = await db.reconcile_orphans()

        assert orphaned_ids == []

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

        orphaned_ids = await db.reconcile_orphans()

        assert len(orphaned_ids) == 3
        run_ids_set = {run.id for run in runs}
        assert set(orphaned_ids) == run_ids_set
        for run in runs:
            updated = await db.get_run(run.id)
            assert updated is not None
            assert updated.status == RunStatus.failed


class TestPushResultsPersistence:
    async def test_push_results_round_trip(self) -> None:
        """push_results survives update_run/get_run/_row_to_run with enum as string."""
        await db.init_db()
        run = Run(item_id="item1", project_name="proj", branch_name="feat/x")
        await db.insert_run(run)

        results = [
            RepoPushStatus(
                repo_name="repos-myrepo",
                branch="feat/x",
                status=PushStatus.pushed,
                local_sha="abc1234",
                remote_sha="abc1234",
                commits_ahead=2,
                dirty=False,
            ),
            RepoPushStatus(
                repo_name="repos-other",
                branch="feat/x",
                status=PushStatus.no_changes,
                local_sha="def5678",
                remote_sha=None,
                commits_ahead=0,
                dirty=False,
            ),
        ]
        push_results_json = json.dumps([r.model_dump(mode="json") for r in results])
        await db.update_run(run.id, push_results=push_results_json)

        retrieved = await db.get_run(run.id)
        assert retrieved is not None
        assert retrieved.push_results is not None
        assert len(retrieved.push_results) == 2

        r0 = retrieved.push_results[0]
        assert r0.repo_name == "repos-myrepo"
        assert r0.status == PushStatus.pushed
        assert r0.status == "pushed"  # StrEnum: value matches string
        assert r0.local_sha == "abc1234"
        assert r0.commits_ahead == 2

        r1 = retrieved.push_results[1]
        assert r1.status == PushStatus.no_changes
        assert r1.commits_ahead == 0

    async def test_push_results_none_by_default(self) -> None:
        """push_results is None for runs that predate push verification."""
        await db.init_db()
        run = Run(item_id="item2", project_name="proj", branch_name="feat/y")
        await db.insert_run(run)

        retrieved = await db.get_run(run.id)
        assert retrieved is not None
        assert retrieved.push_results is None

    async def test_push_results_unpushed_status_survives_as_string(self) -> None:
        """PushStatus.unpushed serializes to the string 'unpushed'."""
        await db.init_db()
        run = Run(item_id="item3", project_name="proj", branch_name="feat/z")
        await db.insert_run(run)

        results = [
            RepoPushStatus(
                repo_name="repos-fail",
                branch="feat/z",
                status=PushStatus.unpushed,
                local_sha=None,
                remote_sha=None,
                commits_ahead=0,
                dirty=False,
            )
        ]
        push_results_json = json.dumps([r.model_dump(mode="json") for r in results])
        await db.update_run(run.id, push_results=push_results_json)

        retrieved = await db.get_run(run.id)
        assert retrieved is not None
        assert retrieved.push_results is not None
        assert retrieved.push_results[0].status == PushStatus.unpushed
        # Verify raw JSON has string value, not int
        raw = json.loads(push_results_json)
        assert raw[0]["status"] == "unpushed"

    async def test_migration_adds_push_results_column(self) -> None:
        """init_db() on a pre-existing DB without push_results gains the column."""
        await db.init_db()

        # Manually drop the push_results column by recreating without it
        db_file = db.db_path()
        conn = sqlite3.connect(db_file)
        try:
            conn.execute("""
                CREATE TABLE runs_legacy (
                    id TEXT PRIMARY KEY,
                    item_id TEXT,
                    project_name TEXT NOT NULL,
                    branch_name TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    started_at TEXT,
                    completed_at TEXT,
                    exit_code INTEGER,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    engine TEXT NOT NULL DEFAULT 'claude-code',
                    engine_actual TEXT,
                    agent_name TEXT,
                    mode TEXT NOT NULL DEFAULT 'build',
                    rollout_id TEXT,
                    workspace_path TEXT
                )
            """)
            conn.execute(
                "INSERT INTO runs_legacy SELECT id, item_id, project_name, "
                "branch_name, status, started_at, completed_at, exit_code, "
                "error, created_at, engine, engine_actual, agent_name, mode, "
                "rollout_id, workspace_path FROM runs"
            )
            conn.execute("DROP TABLE runs")
            conn.execute("ALTER TABLE runs_legacy RENAME TO runs")
            conn.commit()

            # Verify column is gone
            cursor = conn.execute("PRAGMA table_info(runs)")
            col_names = [row[1] for row in cursor.fetchall()]
            assert "push_results" not in col_names
        finally:
            conn.close()

        # Re-run init_db — migration should add the column
        await db.init_db()

        conn2 = sqlite3.connect(db_file)
        try:
            cursor2 = conn2.execute("PRAGMA table_info(runs)")
            col_names2 = [row[1] for row in cursor2.fetchall()]
            assert "push_results" in col_names2
        finally:
            conn2.close()
