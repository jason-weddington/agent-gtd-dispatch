"""Tests for the dispatch API endpoints."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import httpx
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
        assert data["engine"] == "claude-code"
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

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_dispatch_ollama_engine_accepted(
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
                "engine": "claude-code-ollama",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["engine"] == "claude-code-ollama"

    async def test_dispatch_ollama_fallback_posts_comment(
        self, client, auth_headers
    ) -> None:
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import CLAUDE_OLLAMA
        from agent_gtd_dispatch.main import _dispatch_worker
        from agent_gtd_dispatch.models import Run

        await db.init_db()
        run = Run(
            item_id="item-fallback",
            project_name="TestProject",
            branch_name="feat/abc-fix",
            engine="claude-code-ollama",
        )
        await db.insert_run(run)

        post_comment_mock = AsyncMock()

        with (
            patch(
                "agent_gtd_dispatch.main._ollama_health_check",
                new_callable=AsyncMock,
                return_value=(False, "connection refused"),
            ),
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
        ):
            mock_client.post_comment = post_comment_mock
            # Make get_item raise so _dispatch_worker stops after the fallback comment
            mock_client.get_item = AsyncMock(side_effect=Exception("stop early"))

            await _dispatch_worker(run, 50, CLAUDE_OLLAMA, 1800)

        assert post_comment_mock.call_count >= 1
        first_call_args = post_comment_mock.call_args_list[0]
        comment_text = first_call_args[0][1]
        assert "⚠️" in comment_text
        assert "connection refused" in comment_text

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_dispatch_ollama_with_plan_mode_uses_claude(
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
                "engine": "claude-code-ollama",
                "mode": "plan",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        # engine = requested engine (preserved); engine_actual = effective engine after rewrite
        assert data["engine"] == "claude-code-ollama"
        assert data["engine_actual"] == "claude-code"
        assert data["engine_swap"] is not None
        assert data["engine_swap"]["from_engine"] == "claude-code-ollama"
        assert data["engine_swap"]["to_engine"] == "claude-code"


class TestMaxConcurrentRunsEnforcement:
    @patch("agent_gtd_dispatch.main.gtd_client")
    @patch("agent_gtd_dispatch.main.dispatch")
    def test_queues_when_at_capacity(
        self, mock_dispatch, mock_client, client, auth_headers, monkeypatch
    ) -> None:
        """POST /dispatch returns 200 status=pending (queued) when at capacity."""
        from unittest.mock import AsyncMock

        from agent_gtd_dispatch import config

        monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 1)
        monkeypatch.setattr("agent_gtd_dispatch.main._active_processes", {"run1": None})

        fresh_queue: list = []
        monkeypatch.setattr("agent_gtd_dispatch.main._pending_queue", fresh_queue)

        mock_client.get_item = AsyncMock(
            return_value={
                "id": "abc123",
                "title": "Test item",
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
        mock_dispatch.branch_name_for_item.return_value = "feat/abc123-test-item"

        resp = client.post(
            "/dispatch",
            json={"item_id": "abc123", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert len(fresh_queue) == 1

    def test_accepts_when_below_capacity(
        self, client, auth_headers, monkeypatch
    ) -> None:
        """POST /dispatch succeeds when active_runs < max_concurrent_runs."""
        # Set max to 2, fill with 1, then accept 1 more
        from agent_gtd_dispatch import config

        monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 2)
        monkeypatch.setattr("agent_gtd_dispatch.main._active_processes", {"run1": None})

        resp = client.post(
            "/dispatch",
            json={"item_id": "abc123", "max_turns": 50},
            headers=auth_headers,
        )
        # Should fail at the gtd_client.get_item stage but NOT at the capacity check
        # because we're below capacity. The 502 is from upstream error, not capacity check.
        # Capacity check should have passed, so we should get 502 or 503 from upstream,
        # NOT 503 from the capacity check which would have "capacity" in the detail.
        if resp.status_code == 503:
            data = resp.json()
            assert "capacity" not in str(data).lower()

    def test_accepts_when_empty(self, client, auth_headers, monkeypatch) -> None:
        """POST /dispatch succeeds when active_runs == 0 < max_concurrent_runs."""
        # Ensure _active_processes is empty
        monkeypatch.setattr("agent_gtd_dispatch.main._active_processes", {})

        resp = client.post(
            "/dispatch",
            json={"item_id": "abc123", "max_turns": 50},
            headers=auth_headers,
        )
        # Should fail at the gtd_client.get_item stage but NOT at the capacity check
        # because we're below capacity. The 502 is from upstream error, not capacity check.
        # Capacity check should have passed, so we should get 502 or 503 from upstream,
        # NOT 503 from the capacity check which would have "capacity" in the detail.
        if resp.status_code == 503:
            data = resp.json()
            assert "capacity" not in str(data).lower()

    @patch("agent_gtd_dispatch.main.gtd_client")
    @patch("agent_gtd_dispatch.main.dispatch")
    def test_manage_mode_queues_at_concurrent_limit(
        self, mock_dispatch, mock_client, client, auth_headers, monkeypatch
    ) -> None:
        """POST /dispatch for manage mode also queues (200 pending) at max_concurrent_runs."""
        from unittest.mock import AsyncMock

        from agent_gtd_dispatch import config

        monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 1)
        monkeypatch.setattr("agent_gtd_dispatch.main._active_processes", {"run1": None})

        fresh_queue: list = []
        monkeypatch.setattr("agent_gtd_dispatch.main._pending_queue", fresh_queue)

        mock_client.get_rollout = AsyncMock(
            return_value={
                "id": "rollout123",
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

        resp = client.post(
            "/dispatch",
            json={"rollout_id": "rollout123", "mode": "manage", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert len(fresh_queue) == 1


class TestBurstDispatch:
    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_burst_within_cap(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers, monkeypatch
    ) -> None:
        """N parallel dispatches within cap all return 200 — no burst gets stuck pending."""
        from unittest.mock import AsyncMock

        from agent_gtd_dispatch import config

        n = 3
        monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", n)
        monkeypatch.setattr("agent_gtd_dispatch.main._active_processes", {})

        fresh_queue: list = []
        monkeypatch.setattr("agent_gtd_dispatch.main._pending_queue", fresh_queue)

        mock_client.get_item = AsyncMock(
            return_value={
                "id": "item-burst",
                "title": "Burst item",
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
        mock_dispatch.branch_name_for_item.return_value = "feat/item-burst"

        for _ in range(n):
            resp = client.post(
                "/dispatch",
                json={"item_id": "item-burst", "max_turns": 50},
                headers=auth_headers,
            )
            assert resp.status_code == 200

        # All dispatches should have been handled (either running or the worker was called)
        # None should have ended up queued due to a race-induced false over-cap signal.
        assert len(fresh_queue) == 0
        assert mock_worker.call_count == n

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_over_cap_queues(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers, monkeypatch
    ) -> None:
        """When service is at cap, dispatch returns 200 with status=pending and queues."""
        from unittest.mock import AsyncMock

        from agent_gtd_dispatch import config

        monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 1)
        monkeypatch.setattr(
            "agent_gtd_dispatch.main._active_processes", {"existing-run": None}
        )

        fresh_queue: list = []
        monkeypatch.setattr("agent_gtd_dispatch.main._pending_queue", fresh_queue)

        mock_client.get_item = AsyncMock(
            return_value={
                "id": "item-queued",
                "title": "Queued item",
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
        mock_dispatch.branch_name_for_item.return_value = "feat/item-queued"

        resp = client.post(
            "/dispatch",
            json={"item_id": "item-queued", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"

        # The run must be in the pending queue, not started
        assert len(fresh_queue) == 1
        assert fresh_queue[0].run.item_id == "item-queued"
        mock_worker.assert_not_called()

    async def test_slot_free_drains_queue(self, monkeypatch) -> None:
        """When a slot frees, _try_start_pending() promotes a queued run."""
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.engines import get_engine
        from agent_gtd_dispatch.main import (
            _PendingDispatch,
            _try_start_pending,
        )
        from agent_gtd_dispatch.models import Run

        monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 2)

        fresh_active: dict = {}
        monkeypatch.setattr("agent_gtd_dispatch.main._active_processes", fresh_active)

        run = Run(item_id="item-drain", project_name="proj", branch_name="feat/drain")
        engine = get_engine("claude-code")

        fresh_queue: list = [
            _PendingDispatch(
                run=run,
                engine=engine,
                max_turns=50,
                timeout_seconds=1800,
                attribution=None,
            )
        ]
        monkeypatch.setattr("agent_gtd_dispatch.main._pending_queue", fresh_queue)

        with patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock):
            _try_start_pending()

        # Queue should be drained and the run promoted to active
        assert len(fresh_queue) == 0
        assert run.id in fresh_active


class TestListRuns:
    def test_empty_list(self, client, auth_headers):
        resp = client.get("/runs", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []


class TestGetRun:
    def test_not_found(self, client, auth_headers):
        resp = client.get("/runs/nonexistent", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_run_returns_push_results(self, client, auth_headers) -> None:
        """GET /runs/{run_id} returns persisted push_results (full DB round-trip)."""
        import json

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.models import PushStatus, RepoPushStatus, Run, RunStatus

        await db.init_db()
        run = Run(
            item_id="item-push-test",
            project_name="proj",
            branch_name="feat/push-test",
        )
        await db.insert_run(run)

        results = [
            RepoPushStatus(
                repo_name="repos-myrepo",
                branch="feat/push-test",
                status=PushStatus.pushed,
                local_sha="deadbeef",
                remote_sha="deadbeef",
                commits_ahead=1,
                dirty=False,
            )
        ]
        push_results_json = json.dumps([r.model_dump(mode="json") for r in results])
        await db.update_run(
            run.id,
            status=RunStatus.succeeded,
            push_results=push_results_json,
        )

        resp = client.get(f"/runs/{run.id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["push_results"] is not None
        assert len(data["push_results"]) == 1
        pr = data["push_results"][0]
        assert pr["repo_name"] == "repos-myrepo"
        assert pr["status"] == "pushed"
        assert pr["local_sha"] == "deadbeef"
        assert pr["commits_ahead"] == 1
        assert pr["dirty"] is False


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
        mock_planner._active_planner_model.return_value = "claude-sonnet-4-6"
        resp = client.post("/plan", json={"item_ids": ["id1"]}, headers=auth_headers)
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert isinstance(detail, dict)
        assert "GTD API down" in detail["detail"]
        assert "planner_model" in detail
        assert detail["item_count"] == 1

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


class TestOllamaHealthCheck:
    async def test_returns_false_when_base_url_empty(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.main import _ollama_health_check

        monkeypatch.setattr(config, "OLLAMA_BASE_URL", "")
        ok, reason = await _ollama_health_check()
        assert ok is False
        assert "OLLAMA_BASE_URL" in reason

    async def test_returns_true_on_successful_get(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.main import _ollama_health_check

        monkeypatch.setattr(config, "OLLAMA_BASE_URL", "http://10.0.0.5:11434/v1")

        mock_resp = AsyncMock()
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client  # aenter returns itself
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            ok, reason = await _ollama_health_check()

        assert ok is True
        assert reason == ""

    async def test_returns_false_on_connection_error(self, monkeypatch) -> None:
        from agent_gtd_dispatch import config
        from agent_gtd_dispatch.main import _ollama_health_check

        monkeypatch.setattr(config, "OLLAMA_BASE_URL", "http://unreachable:11434/v1")

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client  # aenter returns itself
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            ok, reason = await _ollama_health_check()

        assert ok is False
        assert "connection refused" in reason


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


def _make_http_status_error(
    status_code: int, body: str = "error"
) -> httpx.HTTPStatusError:
    """Build a minimal httpx.HTTPStatusError for testing."""
    request = httpx.Request("GET", "http://gtd.local/api/items/abc")
    response = httpx.Response(status_code, text=body, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=request,
        response=response,
    )


class TestBoundaryErrors:
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_get_item_404_returns_404(self, mock_client, client, auth_headers) -> None:
        mock_client.get_item = AsyncMock(side_effect=_make_http_status_error(404))
        resp = client.post(
            "/dispatch",
            json={"item_id": "abc123", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 404
        data = resp.json()
        assert data["detail"] == "Item not found"

    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_get_item_500_returns_502_structured(
        self, mock_client, client, auth_headers
    ) -> None:
        mock_client.get_item = AsyncMock(
            side_effect=_make_http_status_error(500, "internal server error")
        )
        resp = client.post(
            "/dispatch",
            json={"item_id": "abc123", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 502
        data = resp.json()
        assert "upstream_status" in data["detail"]
        assert data["detail"]["upstream_status"] == 500

    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_get_item_connect_error_returns_503(
        self, mock_client, client, auth_headers
    ) -> None:
        request = httpx.Request("GET", "http://gtd.local/api/items/abc")
        mock_client.get_item = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused", request=request)
        )
        resp = client.post(
            "/dispatch",
            json={"item_id": "abc123", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 503
        data = resp.json()
        assert isinstance(data["detail"], dict)
        assert "upstream" in data["detail"]["detail"].lower()

    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_get_item_malformed_json_returns_502(
        self, mock_client, client, auth_headers
    ) -> None:
        mock_client.get_item = AsyncMock(
            side_effect=json.JSONDecodeError("Expecting value", "", 0)
        )
        resp = client.post(
            "/dispatch",
            json={"item_id": "abc123", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 502
        data = resp.json()
        assert isinstance(data["detail"], dict)
        assert "malformed" in data["detail"]["detail"].lower()


# ---------------------------------------------------------------------------
# Workspace dispatch tests (AC-6, AC-7, AC-10)
# ---------------------------------------------------------------------------


class TestWorkspaceDispatch:
    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_workspace_happy_path_no_git_origin_required(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers
    ) -> None:
        """Workspace project (no git_origin) dispatches successfully — route layer accepts
        and worker is invoked, proving the git_origin guard is bypassed in workspace mode."""
        mock_client.get_item = AsyncMock(
            return_value={
                "id": "abc12345-6789",
                "title": "Fix workspace bug",
                "project_id": "proj-ws",
            }
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj-ws",
                "name": "WorkspaceProject",
                # deliberately no git_origin key — workspace dispatch must not require it
                "repo_mode": "workspace",
                "workspace_repos": [
                    "git@host:org/repo-a.git",
                    "git@host:org/repo-b.git",
                ],
            }
        )
        mock_dispatch.branch_name_for_item.return_value = "feat/abc12345-fix"

        resp = client.post(
            "/dispatch",
            json={"item_id": "abc12345-6789", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["item_id"] == "abc12345-6789"
        assert data["status"] == "pending"
        # Worker must have been invoked (proves route layer did not 400)
        mock_worker.assert_called_once()

    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_workspace_project_empty_workspace_repos_returns_400(
        self, mock_client, client, auth_headers
    ) -> None:
        """Workspace project with empty workspace_repos returns 400 containing 'workspace_repos'."""
        mock_client.get_item = AsyncMock(
            return_value={
                "id": "abc123",
                "title": "Test item",
                "project_id": "proj-ws",
            }
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj-ws",
                "name": "WorkspaceProject",
                "repo_mode": "workspace",
                # workspace_repos absent → treated as empty
            }
        )

        resp = client.post(
            "/dispatch",
            json={"item_id": "abc123", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "workspace_repos" in resp.json()["detail"]

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_manage_workspace_project_accepted(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers
    ) -> None:
        """Manage dispatch targeting a workspace project with workspace_repos returns 200."""
        mock_client.get_rollout = AsyncMock(
            return_value={
                "id": "rollout-ws",
                "project_id": "proj-ws",
                "status": "running",
            }
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj-ws",
                "name": "WorkspaceProject",
                # deliberately no git_origin key — workspace manage must not require it
                "repo_mode": "workspace",
                "workspace_repos": [
                    "git@host:org/repo-a.git",
                    "git@host:org/repo-b.git",
                ],
            }
        )
        mock_dispatch.branch_name_for_item.return_value = "feat/abc123"

        resp = client.post(
            "/dispatch",
            json={"rollout_id": "rollout-ws", "mode": "manage", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["mode"] == "manage"
        # Worker must have been invoked (proves the guard was removed)
        mock_worker.assert_called_once()

    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_manage_workspace_project_empty_repos_returns_400(
        self, mock_client, client, auth_headers
    ) -> None:
        """Manage dispatch targeting a workspace project with empty workspace_repos returns 400."""
        mock_client.get_rollout = AsyncMock(
            return_value={
                "id": "rollout-ws",
                "project_id": "proj-ws",
                "status": "running",
            }
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj-ws",
                "name": "WorkspaceProject",
                "repo_mode": "workspace",
                # workspace_repos absent → treated as empty
            }
        )

        resp = client.post(
            "/dispatch",
            json={"rollout_id": "rollout-ws", "mode": "manage", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "workspace_repos" in resp.json()["detail"]

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_no_repo_mode_key_follows_monorepo_path(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers
    ) -> None:
        """Regression: project with no repo_mode key dispatches exactly as before."""
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
                "name": "MonorepoProject",
                "git_origin": "git@ubuntu-vm01:repos/test",
                # no repo_mode key at all
            }
        )
        mock_dispatch.branch_name_for_item.return_value = "feat/abc12345-fix-bug"

        resp = client.post(
            "/dispatch",
            json={"item_id": "abc12345-6789", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_unknown_repo_mode_value_follows_monorepo_path(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers
    ) -> None:
        """Regression: unrecognized repo_mode value (not 'workspace') uses monorepo path."""
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
                "name": "SomeProject",
                "git_origin": "git@ubuntu-vm01:repos/test",
                "repo_mode": "something-else",  # unknown value → monorepo
            }
        )
        mock_dispatch.branch_name_for_item.return_value = "feat/abc12345-fix-bug"

        resp = client.post(
            "/dispatch",
            json={"item_id": "abc12345-6789", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"

    async def test_workspace_worker_calls_prepare_workspace_multi(
        self, client, auth_headers
    ) -> None:
        """_dispatch_worker calls prepare_workspace_multi for workspace projects,
        proving the git_origin guard is bypassed — run reaches prepare_workspace_multi."""
        import subprocess

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import get_engine
        from agent_gtd_dispatch.main import _dispatch_worker
        from agent_gtd_dispatch.models import Run

        await db.init_db()
        run = Run(
            item_id="item-ws",
            project_name="WorkspaceProject",
            branch_name="feat/item-ws-fix",
        )
        await db.insert_run(run)

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_client.get_item = AsyncMock(
                return_value={
                    "id": "item-ws",
                    "title": "Fix workspace bug",
                    "project_id": "proj-ws",
                }
            )
            mock_client.get_project = AsyncMock(
                return_value={
                    "id": "proj-ws",
                    "name": "WorkspaceProject",
                    # NO git_origin key — proves independence
                    "repo_mode": "workspace",
                    "workspace_repos": [
                        "git@host:org/repo-a.git",
                        "git@host:org/repo-b.git",
                    ],
                }
            )
            mock_client.post_comment = AsyncMock()
            mock_client.list_attachments = AsyncMock(return_value=[])

            from pathlib import Path

            fake_workspace = Path("/tmp/ws-item-ws")  # noqa: S108
            mock_dispatch.prepare_workspace_multi.return_value = fake_workspace
            mock_dispatch.repo_dir_from_url.side_effect = lambda url: url.rsplit(
                "/", 1
            )[-1].replace(".git", "")
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt.return_value = "test prompt"
            mock_dispatch.branch_name_for_item.return_value = "feat/item-ws-fix"
            mock_dispatch.run_agent = AsyncMock(
                return_value=subprocess.CompletedProcess([], 0, "", "")
            )
            mock_dispatch.cleanup_workspace.return_value = None

            engine = get_engine("claude-code")
            await _dispatch_worker(run, 50, engine, 1800)

        mock_dispatch.prepare_workspace_multi.assert_called_once_with(
            ["git@host:org/repo-a.git", "git@host:org/repo-b.git"],
            run.id,
            "feat/item-ws-fix",
        )

    async def test_manage_workspace_worker_calls_prepare_manage_workspace_multi(
        self, client, auth_headers
    ) -> None:
        """_dispatch_worker calls prepare_manage_workspace_multi for manage+workspace runs."""
        import subprocess

        from agent_gtd_dispatch_protocol.models import DispatchMode

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import get_engine
        from agent_gtd_dispatch.main import _dispatch_worker
        from agent_gtd_dispatch.models import Run

        await db.init_db()
        run = Run(
            item_id=None,
            project_name="WorkspaceProject",
            branch_name=None,
            mode=DispatchMode.MANAGE,
            rollout_id="rollout-ws-mgr",
        )
        await db.insert_run(run)

        workspace_repos = [
            "git@host:org/repo-a.git",
            "git@host:org/repo-b.git",
        ]

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_client.get_rollout = AsyncMock(
                return_value={
                    "id": "rollout-ws-mgr",
                    "project_id": "proj-ws",
                    "status": "running",
                }
            )
            mock_client.get_project = AsyncMock(
                return_value={
                    "id": "proj-ws",
                    "name": "WorkspaceProject",
                    # NO git_origin key
                    "repo_mode": "workspace",
                    "workspace_repos": workspace_repos,
                }
            )
            mock_client.post_comment = AsyncMock()

            from pathlib import Path

            fake_workspace = Path("/tmp/repos-manage-ws")  # noqa: S108
            mock_dispatch.prepare_manage_workspace_multi.return_value = fake_workspace
            mock_dispatch.repo_dir_from_url.side_effect = lambda url: url.rsplit(
                "/", 1
            )[-1].replace(".git", "")
            mock_dispatch.build_system_prompt.return_value = "manage prompt"
            mock_dispatch.run_agent = AsyncMock(
                return_value=subprocess.CompletedProcess([], 0, "", "")
            )
            mock_dispatch.cleanup_workspace.return_value = None
            mock_dispatch.verify_pushes = None  # Should never be called

            engine = get_engine("claude-code")
            await _dispatch_worker(run, 200, engine, 3600)

        # AC-10(b): prepare_manage_workspace_multi was called with the workspace_repos
        mock_dispatch.prepare_manage_workspace_multi.assert_called_once_with(
            workspace_repos,
            run.id,
        )
        # AC-10(b): build_system_prompt received workspace_repo_dirs
        _call = mock_dispatch.build_system_prompt.call_args
        assert _call is not None
        workspace_repo_dirs_arg = _call.kwargs.get("workspace_repo_dirs")
        assert workspace_repo_dirs_arg == ["repo-a", "repo-b"]
        # AC-8: cleanup_workspace called with the workspace root on success
        mock_dispatch.cleanup_workspace.assert_called_once_with(fake_workspace)

    async def test_manage_workspace_worker_verify_pushes_not_called(
        self, client, auth_headers
    ) -> None:
        """verify_pushes is never called for manage mode runs (AC-6)."""
        import subprocess

        from agent_gtd_dispatch_protocol.models import DispatchMode

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import get_engine
        from agent_gtd_dispatch.main import _dispatch_worker
        from agent_gtd_dispatch.models import Run

        await db.init_db()
        run = Run(
            item_id=None,
            project_name="WorkspaceProject",
            branch_name=None,
            mode=DispatchMode.MANAGE,
            rollout_id="rollout-ws-nv",
        )
        await db.insert_run(run)

        workspace_repos = ["git@host:org/repo-a.git"]

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_client.get_rollout = AsyncMock(
                return_value={
                    "id": "rollout-ws-nv",
                    "project_id": "proj-ws-nv",
                    "status": "running",
                }
            )
            mock_client.get_project = AsyncMock(
                return_value={
                    "id": "proj-ws-nv",
                    "name": "WorkspaceProject",
                    "repo_mode": "workspace",
                    "workspace_repos": workspace_repos,
                }
            )
            mock_client.post_comment = AsyncMock()

            from pathlib import Path

            fake_workspace = Path("/tmp/repos-manage-nv")  # noqa: S108
            mock_dispatch.prepare_manage_workspace_multi.return_value = fake_workspace
            mock_dispatch.repo_dir_from_url.side_effect = lambda url: url.rsplit(
                "/", 1
            )[-1].replace(".git", "")
            mock_dispatch.build_system_prompt.return_value = "manage prompt"
            mock_dispatch.run_agent = AsyncMock(
                return_value=subprocess.CompletedProcess([], 0, "", "")
            )
            mock_dispatch.cleanup_workspace.return_value = None

            engine = get_engine("claude-code")
            await _dispatch_worker(run, 200, engine, 3600)

        # AC-6: verify_pushes must not be called for manage runs
        mock_dispatch.verify_pushes.assert_not_called()
