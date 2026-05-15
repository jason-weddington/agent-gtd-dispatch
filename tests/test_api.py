"""Tests for the dispatch API endpoints."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

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


class TestHealth:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestAuth:
    def test_no_auth(self, client):
        resp = client.post("/dispatch", json={"item_id": "abc"})
        assert resp.status_code == 401

    def test_bad_auth(self, client):
        resp = client.post(
            "/dispatch",
            json={"item_id": "abc"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401

    def test_list_runs_requires_auth(self, client):
        resp = client.get("/runs")
        assert resp.status_code == 401


class TestDispatch:
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_dispatch_missing_project(self, mock_client, client, auth_headers):
        mock_client.get_item = AsyncMock(
            return_value={"id": "abc123", "title": "Test", "project_id": None}
        )
        resp = client.post(
            "/dispatch",
            json={"item_id": "abc123", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "no project" in resp.json()["detail"].lower()

    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_dispatch_missing_git_origin(self, mock_client, client, auth_headers):
        mock_client.get_item = AsyncMock(
            return_value={"id": "abc123", "title": "Test", "project_id": "proj1"}
        )
        mock_client.get_project = AsyncMock(
            return_value={"id": "proj1", "name": "TestProject", "git_origin": ""}
        )
        resp = client.post(
            "/dispatch",
            json={"item_id": "abc123", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "git_origin" in resp.json()["detail"]

    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_dispatch_success(self, mock_client, mock_dispatch, client, auth_headers):
        mock_client.get_item = AsyncMock(
            return_value={
                "id": "abc12345-6789",
                "title": "Fix bug",
                "project_id": "proj1",
            }
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj1",
                "name": "TestProject",
                "git_origin": "git@ubuntu-vm01:repos/test",
            }
        )
        mock_client.post_comment = AsyncMock()
        mock_dispatch.branch_name_for_item.return_value = "feat/abc12345-fix-bug"

        resp = client.post(
            "/dispatch",
            json={"item_id": "abc12345-6789", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["item_id"] == "abc12345-6789"
        assert data["status"] == "pending"
        assert data["branch_name"] == "feat/abc12345-fix-bug"
        assert data["engine"] == "claude"
        assert data["agent_name"] is None
        assert data["id"]  # has a run ID

    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_dispatch_with_engine_and_agent(
        self, mock_client, mock_dispatch, client, auth_headers
    ):
        mock_client.get_item = AsyncMock(
            return_value={
                "id": "abc12345-6789",
                "title": "Fix bug",
                "project_id": "proj1",
            }
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj1",
                "name": "TestProject",
                "git_origin": "git@ubuntu-vm01:repos/test",
            }
        )
        mock_client.post_comment = AsyncMock()
        mock_dispatch.branch_name_for_item.return_value = "feat/abc12345-fix-bug"

        resp = client.post(
            "/dispatch",
            json={
                "item_id": "abc12345-6789",
                "max_turns": 50,
                "engine": "kiro",
                "agent_name": "my-agent",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["engine"] == "kiro"
        assert data["agent_name"] == "my-agent"

    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_dispatch_unknown_engine(self, mock_client, client, auth_headers):
        mock_client.get_item = AsyncMock(
            return_value={
                "id": "abc123",
                "title": "Test",
                "project_id": "proj1",
            }
        )
        resp = client.post(
            "/dispatch",
            json={"item_id": "abc123", "max_turns": 50, "engine": "gpt-5"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "Unknown engine" in resp.json()["detail"]

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_dispatch_timeout_minutes_passes_computed_seconds(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers
    ):
        from agent_gtd_dispatch import config

        config.TIMEOUT_SECONDS = 3600  # global default — should NOT be used

        mock_client.get_item = AsyncMock(
            return_value={
                "id": "abc12345-6789",
                "title": "Fix bug",
                "project_id": "proj1",
            }
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj1",
                "name": "TestProject",
                "git_origin": "git@ubuntu-vm01:repos/test",
            }
        )
        mock_dispatch.branch_name_for_item.return_value = "feat/abc12345-fix-bug"

        resp = client.post(
            "/dispatch",
            json={"item_id": "abc12345-6789", "max_turns": 50, "timeout_minutes": 30},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        # _dispatch_worker must have been called with timeout_seconds=1800 (30*60)
        _run, _max_turns, _engine, timeout_seconds = mock_worker.call_args.args
        assert timeout_seconds == 1800

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_dispatch_without_timeout_minutes_uses_config_default(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers
    ):
        from agent_gtd_dispatch import config

        config.TIMEOUT_SECONDS = 7200  # arbitrary global default

        mock_client.get_item = AsyncMock(
            return_value={
                "id": "abc12345-6789",
                "title": "Fix bug",
                "project_id": "proj1",
            }
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj1",
                "name": "TestProject",
                "git_origin": "git@ubuntu-vm01:repos/test",
            }
        )
        mock_dispatch.branch_name_for_item.return_value = "feat/abc12345-fix-bug"

        resp = client.post(
            "/dispatch",
            json={"item_id": "abc12345-6789", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        _run, _max_turns, _engine, timeout_seconds = mock_worker.call_args.args
        assert timeout_seconds == 7200

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_manage_mode_uses_manage_timeout_seconds(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers
    ):
        from agent_gtd_dispatch import config

        config.MANAGE_TIMEOUT_SECONDS = 14400
        config.TIMEOUT_SECONDS = 1800  # should NOT be used for manage mode

        mock_client.get_rollout = AsyncMock(
            return_value={
                "id": "wr-abc",
                "project_id": "proj1",
                "status": "running",
            }
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj1",
                "name": "WaveProject",
                "git_origin": "git@ubuntu-vm01:repos/wave-project",
            }
        )
        mock_dispatch.prepare_manage_workspace.return_value = None

        resp = client.post(
            "/dispatch",
            json={
                "max_turns": 200,
                "mode": "manage",
                "rollout_id": "wr-abc",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        _run, _max_turns, _engine, timeout_seconds = mock_worker.call_args.args
        assert timeout_seconds == 14400

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_dispatch_attribution_passed_to_worker(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers
    ):
        mock_client.get_item = AsyncMock(
            return_value={
                "id": "abc12345-6789",
                "title": "Fix bug",
                "project_id": "proj1",
            }
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj1",
                "name": "TestProject",
                "git_origin": "git@ubuntu-vm01:repos/test",
            }
        )
        mock_dispatch.branch_name_for_item.return_value = "feat/abc12345-fix-bug"

        resp = client.post(
            "/dispatch",
            json={
                "item_id": "abc12345-6789",
                "max_turns": 50,
                "attribution": "claude-build-abc12345",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        # _dispatch_worker must have been called with attribution kwarg
        assert mock_worker.call_args.kwargs["attribution"] == "claude-build-abc12345"


class TestListRuns:
    def test_empty_list(self, client, auth_headers):
        resp = client.get("/runs", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []


class TestGetRun:
    def test_not_found(self, client, auth_headers):
        resp = client.get("/runs/nonexistent", headers=auth_headers)
        assert resp.status_code == 404


class TestCancelRun:
    def test_cancel_not_found(self, client, auth_headers):
        resp = client.post("/runs/nonexistent/cancel", headers=auth_headers)
        assert resp.status_code == 404


class TestDispatchManageMode:
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_manage_without_rollout_id_returns_400(
        self, mock_client, client, auth_headers
    ):
        resp = client.post(
            "/dispatch",
            json={"item_id": "abc123", "max_turns": 50, "mode": "manage"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "rollout_id" in resp.json()["detail"]

    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_manage_with_rollout_id_accepted(
        self, mock_client, mock_dispatch, client, auth_headers
    ):
        mock_client.get_rollout = AsyncMock(
            return_value={
                "id": "wr-abc",
                "project_id": "proj1",
                "status": "running",
            }
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj1",
                "name": "WaveProject",
                "git_origin": "git@ubuntu-vm01:repos/wave-project",
            }
        )
        mock_client.post_comment = AsyncMock()
        mock_dispatch.build_system_prompt.return_value = "manage prompt"
        mock_dispatch.prepare_manage_workspace.return_value = None

        resp = client.post(
            "/dispatch",
            json={
                "max_turns": 200,
                "mode": "manage",
                "rollout_id": "wr-abc",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["item_id"] is None
        assert data["mode"] == "manage"
        assert data["rollout_id"] == "wr-abc"
        assert data["branch_name"] is None

    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_build_mode_without_item_id_returns_400(
        self, mock_client, client, auth_headers
    ):
        resp = client.post(
            "/dispatch",
            json={"max_turns": 50, "mode": "build"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "item_id" in resp.json()["detail"]


class TestPlan:
    @patch("agent_gtd_dispatch.main.rollout_planner")
    def test_no_auth_returns_401(self, mock_planner, client):
        resp = client.post("/plan", json={"item_ids": ["id1"]})
        assert resp.status_code == 401

    @patch("agent_gtd_dispatch.main.rollout_planner")
    def test_valid_request_returns_rollout_plan(
        self, mock_planner, client, auth_headers
    ):
        from agent_gtd_dispatch.models import DagEdge, RolloutPlan

        mock_planner.plan_rollout = AsyncMock(
            return_value=RolloutPlan(
                nodes=["id1", "id2"],
                edges=[DagEdge(from_item_id="id1", to_item_id="id2")],
                planner_model="claude-sonnet-4-6",
            )
        )
        resp = client.post(
            "/plan", json={"item_ids": ["id1", "id2"]}, headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["nodes"] == ["id1", "id2"]
        assert len(data["edges"]) == 1
        assert data["edges"][0]["from_item_id"] == "id1"
        assert data["edges"][0]["to_item_id"] == "id2"
        assert data["planner_model"] == "claude-sonnet-4-6"

    @patch("agent_gtd_dispatch.main.rollout_planner")
    def test_plan_rollout_raises_returns_502(self, mock_planner, client, auth_headers):
        mock_planner.plan_rollout = AsyncMock(side_effect=Exception("GTD API down"))
        resp = client.post("/plan", json={"item_ids": ["id1"]}, headers=auth_headers)
        assert resp.status_code == 502
        assert "GTD API down" in resp.json()["detail"]

    @patch("agent_gtd_dispatch.main.rollout_planner")
    def test_empty_item_ids_returns_422(self, mock_planner, client, auth_headers):
        resp = client.post("/plan", json={"item_ids": []}, headers=auth_headers)
        assert resp.status_code == 422


class TestTranscriptEndpoint:
    def test_not_found(self, client, auth_headers) -> None:
        resp = client.get("/runs/nonexistent/transcript", headers=auth_headers)
        assert resp.status_code == 404

    def test_requires_auth(self, client) -> None:
        resp = client.get("/runs/xxx/transcript")
        assert resp.status_code == 401

    async def test_no_workspace_returns_no_transcript(
        self, client, auth_headers
    ) -> None:
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.models import Run

        await db.init_db()
        run = Run(item_id="i1", project_name="p", branch_name="b")
        await db.insert_run(run)

        resp = client.get(f"/runs/{run.id}/transcript", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["text"] == "no transcript yet"
        assert data["last_modified"] is None
        assert data["total_lines"] == 0

    async def test_returns_last_n_lines(self, client, auth_headers, tmp_path) -> None:
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.models import Run

        await db.init_db()
        run = Run(
            item_id="i1",
            project_name="p",
            branch_name="b",
            workspace_path=str(tmp_path),
        )
        await db.insert_run(run)

        # Write a transcript with 10 lines
        transcript = tmp_path / "transcript.txt"
        transcript.write_text("\n".join(f"line {i}" for i in range(10)))

        resp = client.get(f"/runs/{run.id}/transcript?lines=3", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_lines"] == 10
        assert data["text"] == "line 7\nline 8\nline 9"
        assert data["last_modified"] is not None

    async def test_missing_transcript_file_returns_no_transcript(
        self, client, auth_headers, tmp_path
    ) -> None:
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.models import Run

        await db.init_db()
        run = Run(
            item_id="i1",
            project_name="p",
            branch_name="b",
            workspace_path=str(tmp_path),  # dir exists but no transcript.txt
        )
        await db.insert_run(run)

        resp = client.get(f"/runs/{run.id}/transcript", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["text"] == "no transcript yet"


class TestStartupReconciliation:
    async def test_orphaned_runs_marked_failed_on_startup(self, auth_headers) -> None:
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.main import app
        from agent_gtd_dispatch.models import Run, RunStatus

        # Seed the DB with an orphaned "running" run before starting the app
        await db.init_db()
        run = Run(item_id="item1", project_name="proj", branch_name="feat/x")
        await db.insert_run(run)
        await db.update_run(run.id, status=RunStatus.running)

        # Starting the TestClient triggers the lifespan (reconcile_orphans)
        with TestClient(app) as c:
            resp = c.get(f"/runs/{run.id}", headers=auth_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "failed"
            assert data["error"] == "Service restarted while run was active"
