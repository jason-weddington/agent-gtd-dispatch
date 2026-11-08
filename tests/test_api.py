"""Tests for the dispatch API endpoints."""

from __future__ import annotations

import json
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_gtd_dispatch import main


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
        body = resp.json()
        assert set(body) == {"status", "active_runs", "restart_blocking_runs"}
        assert isinstance(body["restart_blocking_runs"], int)


class TestParkedRolloutLoopsExcludedFromActiveRuns:
    def test_info_and_health_exclude_parked_loops(self, client, monkeypatch) -> None:
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            "agent_gtd_dispatch.main._active_processes",
            {"build-1": MagicMock(), "loop-1": MagicMock()},
        )
        monkeypatch.setattr("agent_gtd_dispatch.main._parked_rollout_loops", {"loop-1"})

        info = client.get("/info").json()
        assert info["active_runs"] == 1
        assert info["parked_rollout_loops"] == 1
        assert client.get("/health").json()["active_runs"] == 1

    def test_parked_id_without_live_task_is_not_counted(
        self, client, monkeypatch
    ) -> None:
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            "agent_gtd_dispatch.main._active_processes", {"build-1": MagicMock()}
        )
        monkeypatch.setattr("agent_gtd_dispatch.main._parked_rollout_loops", {"gone"})

        info = client.get("/info").json()
        assert info["active_runs"] == 1
        assert info["parked_rollout_loops"] == 0


class TestRestartBlockingRuns:
    @staticmethod
    def _setup(monkeypatch, *, loop=None, state=None, procs=None, queue=()):
        import types  # noqa: F401
        from unittest.mock import MagicMock

        from agent_gtd_dispatch import main

        active = {k: MagicMock() for k in (procs or [])}
        loop_ids = set()
        states = {}
        if loop is not None:
            active[loop] = MagicMock()
            loop_ids.add(loop)
        if state is not None:
            states[loop or "orphan"] = state
        monkeypatch.setattr(main, "_active_processes", active)
        monkeypatch.setattr(main, "_loop_run_ids", loop_ids)
        monkeypatch.setattr(main, "_rollout_loop_states", states)
        monkeypatch.setattr(main, "_pending_queue", list(queue))
        monkeypatch.setattr(main, "_parked_rollout_loops", set())

    @staticmethod
    def _task(done: bool):
        from unittest.mock import MagicMock

        return MagicMock(done=MagicMock(return_value=done))

    def _land(self, *, done=False, probe="set", **lander_kw):
        import types

        from agent_gtd_dispatch import main

        if probe is None:
            return main._ActiveLand("i1", 0, self._task(done), 0.0, probe=None)
        base = {
            "lease_ever_acquired": False,
            "lease_id": None,
            "pushed": {},
            "in_acquire_call": False,
        }
        returned = lander_kw.pop("lander_returned", False)
        lander = (
            None
            if lander_kw.pop("no_lander", False)
            else types.SimpleNamespace(**{**base, **lander_kw})
        )
        return main._ActiveLand(
            "i1",
            0,
            self._task(done),
            0.0,
            probe=main._LandProbe(lander=lander, lander_returned=returned),
        )

    def _state(self, *, land=None, reviews=(), queued=False):
        from unittest.mock import MagicMock

        from agent_gtd_dispatch import main

        st = main._RolloutLoopState()
        st.active_land = land
        for i, done in enumerate(reviews):
            st.reviews[f"i{i}"] = main._ReviewEntry(
                item_id=f"i{i}",
                slot=0,
                task=self._task(done),
                item={},
                pre_review_origin={},
                started_at=0.0,
            )
        if queued:
            st.land_queue.append(main._QueuedLand("i2", 1, MagicMock(), {}, {}, 0.0))
        return st

    def _check(self, client, total, **comp):
        info = client.get("/info").json()
        assert info["restart_blocking_runs"] == total
        assert info["restart_blocking_runs"] == sum(info["restart_blocking"].values())
        assert list(info["restart_blocking"]) == [
            "slot_runs",
            "queued_runs",
            "reviews_in_flight",
            "lease_holding_lands",
        ]
        for k, v in comp.items():
            assert info["restart_blocking"][k] == v
        assert client.get("/health").json()["restart_blocking_runs"] == total
        return info

    def test_pre_lease_loops_not_counted(self, client, monkeypatch) -> None:
        st = self._state(land=self._land(no_lander=True))
        self._setup(monkeypatch, loop="loop-1", state=st)
        info = self._check(client, 0)
        assert info["active_runs"] == 1
        st = self._state(land=self._land())
        self._setup(monkeypatch, loop="loop-1", state=st)
        self._check(client, 0)

    def test_reviews(self, client, monkeypatch) -> None:
        self._setup(monkeypatch, loop="loop-1", state=self._state(reviews=[False]))
        self._check(client, 1, reviews_in_flight=1)
        self._setup(monkeypatch, loop="loop-1", state=self._state(reviews=[True]))
        self._check(client, 0)

    def test_lands(self, client, monkeypatch) -> None:
        cases = [
            (self._land(lease_id="L1"), 1),
            (self._land(pushed={"repo": "sha"}), 1),
            (self._land(probe=None), 1),
            (self._land(lease_ever_acquired=True), 1),
            (self._land(lander_returned=True), 1),
            (self._land(done=True, lease_id="L1"), 0),
            (self._land(done=True, probe=None), 0),
            (self._land(in_acquire_call=True), 1),
        ]
        for land, expected in cases:
            self._setup(monkeypatch, loop="loop-1", state=self._state(land=land))
            self._check(client, expected, lease_holding_lands=expected)

    def test_build_queue_parked_landqueue(self, client, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from agent_gtd_dispatch import main

        self._setup(monkeypatch, procs=["b1"])
        self._check(client, 1, slot_runs=1)
        self._setup(monkeypatch, queue=[MagicMock()])
        self._check(client, 1, queued_runs=1)
        self._setup(monkeypatch, loop="loop-1", state=self._state())
        monkeypatch.setattr(main, "_parked_rollout_loops", {"loop-1"})
        info = self._check(client, 0)
        assert info["active_runs"] == 0
        self._setup(monkeypatch, loop="loop-1", state=self._state(queued=True))
        self._check(client, 0)

    def test_orphan_state_not_counted_and_audited(
        self, client, monkeypatch, caplog
    ) -> None:
        self._setup(monkeypatch, state=self._state(reviews=[False]))
        with caplog.at_level("ERROR"):
            self._check(client, 0)
        assert "rollout-loop state leak" in caplog.text

    def test_missing_state_audited(self, client, monkeypatch, caplog) -> None:
        self._setup(monkeypatch, loop="loop-1")
        with caplog.at_level("ERROR"):
            self._check(client, 0)
        assert "rollout-loop state missing" in caplog.text

    def test_shutdown_log(self, monkeypatch, caplog) -> None:
        import logging

        from agent_gtd_dispatch import main

        self._setup(monkeypatch, loop="loop-1", state=self._state(reviews=[False]))
        with caplog.at_level(logging.INFO):
            main._log_shutdown_restart_blocking()
        recs = [r for r in caplog.records if "shutdown: restart_blocking" in r.message]
        assert recs[0].levelno == logging.WARNING
        assert "reviews_in_flight=1" in recs[0].message
        caplog.clear()
        self._setup(monkeypatch, loop="loop-1", state=self._state())
        with caplog.at_level(logging.INFO):
            main._log_shutdown_restart_blocking()
        recs = [r for r in caplog.records if "shutdown: restart_blocking" in r.message]
        assert recs[0].levelno == logging.INFO
        assert "restart_blocking_runs=0" in recs[0].message


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
                "git_origin": "git@git.example.com:repos/test",
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
                "git_origin": "git@git.example.com:repos/test",
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
                "git_origin": "git@git.example.com:repos/test",
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
                "git_origin": "git@git.example.com:repos/test",
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

    @patch("agent_gtd_dispatch.main._start_rollout_loop")
    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_manage_mode_drives_the_worker_wave_loop(
        self, mock_client, mock_dispatch, mock_worker, mock_loop, client, auth_headers
    ):
        """A manage dispatch starts the worker-driven loop, not a resident agent."""
        from agent_gtd_dispatch import config

        config.MANAGE_TIMEOUT_SECONDS = 14400
        config.TIMEOUT_SECONDS = 1800

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
                "git_origin": "git@git.example.com:repos/wave-project",
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
        mock_worker.assert_not_called()
        mock_loop.assert_called_once()
        started_run = mock_loop.call_args.args[0]
        assert started_run.mode == "manage"
        assert started_run.rollout_id == "wr-abc"
        assert started_run.engine == "rollout-loop"
        assert started_run.engine_actual == "rollout-loop"
        data = resp.json()
        assert data["engine"] == "rollout-loop"
        assert data["engine_actual"] == "rollout-loop"

    def test_rollout_loop_engine_constant(self):
        from agent_gtd_dispatch import config

        assert config.ROLLOUT_LOOP_ENGINE == "rollout-loop"

    @patch("agent_gtd_dispatch.main._start_rollout_loop")
    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_manage_without_rollout_id_is_rejected_not_relabelled(
        self, mock_client, mock_dispatch, mock_worker, mock_loop, client, auth_headers
    ):
        mock_client.get_item = AsyncMock(
            return_value={"id": "abc12345-6789", "title": "T", "project_id": "proj1"}
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj1",
                "name": "P",
                "git_origin": "git@git.example.com:repos/p",
            }
        )
        resp = client.post(
            "/dispatch",
            json={
                "item_id": "abc12345-6789",
                "max_turns": 200,
                "mode": "manage",
                "engine": "claude-code",
            },
            headers=auth_headers,
        )
        # The host refuses a manage dispatch with no rollout_id outright, so no
        # row exists that could be mislabelled; the relabel keys on rollout_id.
        assert resp.status_code == 400
        mock_loop.assert_not_called()

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
                "git_origin": "git@git.example.com:repos/test",
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
                "git_origin": "git@git.example.com:repos/test",
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
                "git_origin": "git@git.example.com:repos/test",
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


class TestDispatchCallbackToken:
    """Regression: per-run callback_token forwarded to handler-time get_item.

    The bug (2026-06-22): a non-admin user dispatching an item in their own
    project got 404 'Item not found' because the synchronous handler-time
    get_item ran with the static admin key, which scope-fails for items
    the admin does not own/share. Fix: forward body.callback_token to
    gtd_client.get_item / get_project / get_rollout in the dispatch handler.

    Test isolation: POST /dispatch schedules a real background task
    (_dispatch_worker in item mode; _start_rollout_loop -> _rollout_loop_worker
    -> _drive_rollout in manage mode) that re-calls gtd_client.get_item /
    get_rollout / get_project with the same callback_token. Unpatched, that
    task races the assert_awaited_once_with checks under CPU load, so every
    test here patches the background launch. The lifespan
    _readopt_running_rollouts sweep is NOT a caller: it fails on
    list_running_rollouts() and returns before any get_rollout.
    """

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_callback_token_passed_to_handler_get_item(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers
    ):
        """POST /dispatch with callback_token → handler-time get_item gets token=<jwt>."""
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
                "git_origin": "git@git.example.com:repos/test",
            }
        )
        mock_client.post_comment = AsyncMock()
        mock_dispatch.branch_name_for_item.return_value = "feat/abc12345-fix-bug"

        resp = client.post(
            "/dispatch",
            json={
                "item_id": "abc12345-6789",
                "max_turns": 50,
                "callback_token": "eyJ-per-run-jwt",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        # The bug locus: handler-time get_item MUST be called with token=<jwt>
        mock_client.get_item.assert_awaited_once_with(
            "abc12345-6789", token="eyJ-per-run-jwt"
        )
        # And get_project should also forward the token
        mock_client.get_project.assert_awaited_once_with(
            "proj1", token="eyJ-per-run-jwt"
        )

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_no_callback_token_passes_none(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers
    ):
        """Back-compat: omitted callback_token → handler-time get_item gets token=None.

        gtd_client._request then falls back to config.AGENT_GTD_API_KEY,
        preserving today's admin-dispatch path byte-for-byte.
        """
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
                "git_origin": "git@git.example.com:repos/test",
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
        mock_client.get_item.assert_awaited_once_with("abc12345-6789", token=None)
        mock_client.get_project.assert_awaited_once_with("proj1", token=None)

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_callback_token_persisted_on_run(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers
    ):
        """The token is stored on the Run so worker-flow calls can use it."""
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
                "git_origin": "git@git.example.com:repos/test",
            }
        )
        mock_dispatch.branch_name_for_item.return_value = "feat/abc12345-fix-bug"

        resp = client.post(
            "/dispatch",
            json={
                "item_id": "abc12345-6789",
                "max_turns": 50,
                "callback_token": "eyJ-run-jwt-stored",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        # Run object passed to _dispatch_worker carries the callback token
        run_arg = mock_worker.call_args.args[0]
        assert run_arg.callback_token == "eyJ-run-jwt-stored"  # noqa: S105

    @patch("agent_gtd_dispatch.main._start_rollout_loop")
    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_manage_mode_callback_token_forwarded_to_get_rollout(
        self, mock_client, mock_dispatch, mock_worker, mock_loop, client, auth_headers
    ):
        """Manage-mode handler-time get_rollout/get_project forward callback_token too."""
        mock_client.get_rollout = AsyncMock(
            return_value={"id": "wr-abc", "project_id": "proj1", "status": "running"}
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj1",
                "name": "TestProject",
                "git_origin": "git@git.example.com:repos/test",
            }
        )

        resp = client.post(
            "/dispatch",
            json={
                "max_turns": 200,
                "mode": "manage",
                "rollout_id": "wr-abc",
                "callback_token": "eyJ-manage-jwt",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        mock_client.get_rollout.assert_awaited_once_with(
            "wr-abc", token="eyJ-manage-jwt"
        )
        mock_client.get_project.assert_awaited_once_with(
            "proj1", token="eyJ-manage-jwt"
        )
        # The handler hands the run (with its token) to the stubbed loop.
        mock_loop.assert_called_once()
        started_run = mock_loop.call_args.args[0]
        assert started_run.callback_token == "eyJ-manage-jwt"  # noqa: S105
        assert started_run.rollout_id == "wr-abc"


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
                "git_origin": "git@git.example.com:repos/test",
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

    @patch("agent_gtd_dispatch.main._rollout_loop_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.gtd_client")
    @patch("agent_gtd_dispatch.main.dispatch")
    def test_manage_mode_starts_at_concurrent_limit(
        self,
        mock_dispatch,
        mock_client,
        _mock_worker,
        client,
        auth_headers,
        monkeypatch,
    ) -> None:
        """A rollout loop starts immediately even on a full host."""
        from agent_gtd_dispatch import config

        monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 1)
        monkeypatch.setattr("agent_gtd_dispatch.main._active_processes", {"run1": None})
        monkeypatch.setattr("agent_gtd_dispatch.main._loop_run_ids", set())
        monkeypatch.setattr("agent_gtd_dispatch.main._rollout_loops", {})

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
                "git_origin": "git@git.example.com:repos/wave-project",
            }
        )

        resp = client.post(
            "/dispatch",
            json={"rollout_id": "rollout123", "mode": "manage", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert fresh_queue == []
        from agent_gtd_dispatch import main

        assert "rollout123" in main._rollout_loops
        assert resp.json()["id"] in main._loop_run_ids


class TestSlotAccounting:
    def test_slot_count_excludes_loops_and_parked(self, monkeypatch) -> None:
        from agent_gtd_dispatch import main

        monkeypatch.setattr(
            main, "_active_processes", {"b": None, "l1": None, "l2": None}
        )
        monkeypatch.setattr(main, "_loop_run_ids", {"l1", "l2"})
        monkeypatch.setattr(main, "_parked_rollout_loops", {"l2"})
        assert main._slot_holding_count() == 1

    def test_try_start_pending_ignores_loops(self, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from agent_gtd_dispatch import config, main

        monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 4)
        procs = {f"b{i}": MagicMock() for i in range(4)}
        procs.update({"l1": MagicMock(), "l2": MagicMock()})
        monkeypatch.setattr(main, "_active_processes", procs)
        monkeypatch.setattr(main, "_loop_run_ids", {"l1", "l2"})
        monkeypatch.setattr(main, "_parked_rollout_loops", set())
        pending = main._PendingDispatch(
            run=MagicMock(id="q1", mode="build", rollout_id=None),
            engine=MagicMock(),
            max_turns=1,
            timeout_seconds=1,
            attribution=None,
        )
        queue = [pending]
        monkeypatch.setattr(main, "_pending_queue", queue)
        del procs["l1"]
        main._try_start_pending()
        assert queue == [pending]  # builds still full
        del procs["b0"]

        async def _go() -> None:
            with patch.object(main, "_dispatch_worker", new=AsyncMock()):
                main._try_start_pending()
            for t in list(procs.values()):
                if hasattr(t, "cancel") and not isinstance(t, MagicMock):
                    await t

        import asyncio

        asyncio.run(_go())
        assert queue == []
        assert "q1" in procs

    def test_info_reports_slot_fields(self, client, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from agent_gtd_dispatch import main

        monkeypatch.setattr(
            main,
            "_active_processes",
            {"b": MagicMock(), "l1": MagicMock(), "l2": MagicMock()},
        )
        monkeypatch.setattr(main, "_loop_run_ids", {"l1", "l2"})
        monkeypatch.setattr(main, "_parked_rollout_loops", set())
        info = client.get("/info").json()
        assert info["active_runs"] == 3
        assert info["slot_runs"] == 1
        assert info["active_loops"] == 2
        assert info["queued_runs"] == len(main._pending_queue)
        assert client.get("/health").json()["active_runs"] == 3
        monkeypatch.setattr(main, "_parked_rollout_loops", {"l1"})
        info = client.get("/info").json()
        assert info["active_runs"] == 2
        assert info["parked_rollout_loops"] == 1

    def test_audit_run_slots_logs(self, monkeypatch, caplog) -> None:
        from unittest.mock import MagicMock

        from agent_gtd_dispatch import config, main

        monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 1)
        monkeypatch.setattr(
            main, "_active_processes", {"b1": MagicMock(), "b2": MagicMock(), "l": 1}
        )
        monkeypatch.setattr(main, "_loop_run_ids", {"ghost"})
        monkeypatch.setattr(main, "_rollout_loops", {"r": MagicMock(id="l")})
        with caplog.at_level("ERROR"):
            main._audit_run_slots()
        text = caplog.text
        assert "run-slot over-admit" in text
        assert "rollout-loop id leak run=ghost" in text
        assert "rollout-loop untracked run=l" in text


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
                "git_origin": "git@git.example.com:repos/test",
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
                "git_origin": "git@git.example.com:repos/test",
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


class TestReleaseQueued:
    @staticmethod
    def _queue(client, run_id="rq-1", n=1, engine_name="claude-code"):
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import get_engine
        from agent_gtd_dispatch.models import Run

        entries = []
        for i in range(n):
            run = Run(id=f"{run_id}-{i}" if n > 1 else run_id, project_name="P")
            client.portal.call(db.insert_run, run)
            main._engine_slot_holders.setdefault(engine_name, set()).add(run.id)
            entries.append(
                main._PendingDispatch(
                    run=run,
                    engine=get_engine(engine_name),
                    max_turns=5,
                    timeout_seconds=60,
                    attribution=None,
                )
            )
        return entries

    def test_released_and_never_promoted(
        self, client, auth_headers, monkeypatch, caplog
    ) -> None:
        from agent_gtd_dispatch import config

        monkeypatch.setattr(main, "_engine_slot_holders", {})
        entries = self._queue(client)
        queue = list(entries)
        monkeypatch.setattr(main, "_pending_queue", queue)
        monkeypatch.setattr(main, "_active_processes", {})
        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
            caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"),
        ):
            mock_client.post_comment = AsyncMock()
            resp = client.post("/runs/rq-1/release-queued", headers=auth_headers)
            assert resp.status_code == 200
            assert resp.json()["status"] == "cancelled"
            assert (
                resp.json()["error"]
                == "host draining: queued run released for re-placement"
            )
            assert queue == []
            assert "rq-1" not in main._engine_slot_holders["claude-code"]
            mock_client.post_comment.assert_not_called()
            caplog.clear()
            main._audit_engine_slots()
            assert "engine-slot leak" not in caplog.text
            monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 5)
            with patch("agent_gtd_dispatch.main._dispatch_worker") as worker:
                main._try_start_pending()
                worker.assert_not_called()
            assert main._active_processes == {}

    def test_success_log_line(self, client, auth_headers, monkeypatch, caplog) -> None:
        monkeypatch.setattr(main, "_engine_slot_holders", {})
        monkeypatch.setattr(main, "_pending_queue", self._queue(client))
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            client.post("/runs/rq-1/release-queued", headers=auth_headers)
        assert (
            "release-queued run=rq-1 queue_depth_before=1 queue_depth_after=0"
            " engine=claude-code"
        ) in caplog.text

    def test_running_run_gets_409(
        self, client, auth_headers, monkeypatch, caplog
    ) -> None:
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.models import Run

        monkeypatch.setattr(main, "_pending_queue", [])
        run = Run(id="rq-run", project_name="P", status="running")
        client.portal.call(db.insert_run, run)
        task = MagicMock()
        monkeypatch.setattr(main, "_active_processes", {"rq-run": task})
        holders = {"claude-code": {"rq-run"}}
        monkeypatch.setattr(main, "_engine_slot_holders", holders)
        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            resp = client.post("/runs/rq-run/release-queued", headers=auth_headers)
        assert resp.status_code == 409
        assert resp.json()["detail"] == {"reason": "not_queued"}
        task.cancel.assert_not_called()
        assert holders == {"claude-code": {"rq-run"}}
        row = client.get("/runs/rq-run", headers=auth_headers).json()
        assert row["status"] == "running"
        assert "release-queued refused run=rq-run state=running" in caplog.text

    def test_unknown_run_404(self, client, auth_headers) -> None:
        resp = client.post("/runs/nope/release-queued", headers=auth_headers)
        assert resp.status_code == 404

    def test_requires_api_key(self, client) -> None:
        a = client.post("/runs/x/release-queued")
        b = client.post("/runs/x/cancel")
        assert a.status_code == b.status_code
        assert a.status_code in (401, 403)

    def test_fifo_promotion_unchanged(self, client, monkeypatch) -> None:
        from agent_gtd_dispatch import config

        monkeypatch.setattr(main, "_engine_slot_holders", {})
        entries = self._queue(client, run_id="ff", n=2)
        monkeypatch.setattr(main, "_pending_queue", list(entries))
        monkeypatch.setattr(main, "_active_processes", {})
        monkeypatch.setattr(config, "MAX_CONCURRENT_RUNS", 5)
        started: list[str] = []

        async def fake_worker(run, *a, **k):
            started.append(run.id)

        async def go() -> None:
            import asyncio

            with patch("agent_gtd_dispatch.main._dispatch_worker", fake_worker):
                main._try_start_pending()
                await asyncio.sleep(0.05)

        client.portal.call(go)
        assert started == ["ff-0", "ff-1"]


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
                "git_origin": "git@git.example.com:repos/wave-project",
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

    @patch("agent_gtd_dispatch.main._start_rollout_loop")
    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_manage_workspace_project_accepted(
        self, mock_client, mock_dispatch, mock_worker, mock_loop, client, auth_headers
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
        # The worker-driven wave loop must have been started (proves the guard
        # was removed). A manage dispatch no longer spawns a resident agent.
        mock_loop.assert_called_once()
        mock_worker.assert_not_called()

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
                "git_origin": "git@git.example.com:repos/test",
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
                "git_origin": "git@git.example.com:repos/test",
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


# ---------------------------------------------------------------------------
# Preflight item-fetch guard tests
# ---------------------------------------------------------------------------


class TestPreflightItemFetch:
    """Preflight guard: abort before clone/spawn when item is not visible.

    Tests the three preflight outcomes required by the acceptance criteria:
    - success → run proceeds normally
    - 404/403 (authoritative) → run aborted before clone/spawn, error surfaced
    - 5xx (transient) → run is NOT aborted via the preflight path
    """

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_preflight_success_run_proceeds(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers
    ) -> None:
        """Preflight success (200) → _dispatch_worker is invoked normally."""
        mock_client.get_item = AsyncMock(
            return_value={"id": "item-pf-ok", "title": "OK item", "project_id": "proj1"}
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj1",
                "name": "TestProject",
                "git_origin": "git@git.example.com:repos/test",
            }
        )
        mock_dispatch.branch_name_for_item.return_value = "feat/item-pf-ok"

        resp = client.post(
            "/dispatch",
            json={"item_id": "item-pf-ok", "max_turns": 50},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        mock_worker.assert_called_once()

    async def test_preflight_404_aborts_before_clone_and_spawn(self) -> None:
        """Preflight 404 → run marked failed before workspace clone or agent spawn.

        Verifies:
        - run.status == failed
        - run.error contains 'preflight'
        - post_comment called with the preflight error (service-credential reporting)
        - prepare_workspace* NOT called (abort happened before clone)
        - run_agent NOT called (abort happened before spawn)
        """
        from unittest.mock import AsyncMock, patch

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import get_engine
        from agent_gtd_dispatch.main import _dispatch_worker
        from agent_gtd_dispatch.models import Run, RunStatus

        await db.init_db()
        run = Run(
            item_id="item-pf-404",
            project_name="TestProject",
            branch_name="feat/item-pf-404",
            engine="claude-code",
            callback_token="eyJ-run-token",
        )
        await db.insert_run(run)

        post_comment_mock = AsyncMock()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_client.get_item = AsyncMock(side_effect=_make_http_status_error(404))
            mock_client.is_authoritative_item_error = lambda exc: (
                exc.response.status_code in (401, 403, 404)
            )
            mock_client.post_comment = post_comment_mock

            engine = get_engine("claude-code")
            await _dispatch_worker(run, 50, engine, 1800)

        # Run must be marked failed
        final_run = await db.get_run(run.id)
        assert final_run is not None
        assert final_run.status == RunStatus.failed
        # Error must name the preflight as the cause
        assert "preflight" in (final_run.error or "").lower()

        # Comment must have been posted (service-credential reporting)
        assert post_comment_mock.call_count >= 1
        comment_text = post_comment_mock.call_args_list[0][0][1]
        assert "preflight" in comment_text.lower()

        # Workspace clone must NOT have happened
        mock_dispatch.prepare_workspace.assert_not_called()
        mock_dispatch.prepare_workspace_multi.assert_not_called()

        # Agent spawn must NOT have happened
        mock_dispatch.run_agent.assert_not_called()

    async def test_preflight_403_aborts_with_actionable_error(self) -> None:
        """Preflight 403 (not authorised) → same abort behaviour as 404."""
        from unittest.mock import AsyncMock, patch

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import get_engine
        from agent_gtd_dispatch.main import _dispatch_worker
        from agent_gtd_dispatch.models import Run, RunStatus

        await db.init_db()
        run = Run(
            item_id="item-pf-403",
            project_name="TestProject",
            branch_name="feat/item-pf-403",
            engine="claude-code",
            callback_token="eyJ-run-token",
        )
        await db.insert_run(run)

        post_comment_mock = AsyncMock()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_client.get_item = AsyncMock(side_effect=_make_http_status_error(403))
            mock_client.is_authoritative_item_error = lambda exc: (
                exc.response.status_code in (401, 403, 404)
            )
            mock_client.post_comment = post_comment_mock

            engine = get_engine("claude-code")
            await _dispatch_worker(run, 50, engine, 1800)

        final_run = await db.get_run(run.id)
        assert final_run is not None
        assert final_run.status == RunStatus.failed
        assert "preflight" in (final_run.error or "").lower()
        assert post_comment_mock.call_count >= 1
        mock_dispatch.prepare_workspace.assert_not_called()
        mock_dispatch.run_agent.assert_not_called()

    async def test_preflight_5xx_does_not_abort_via_preflight_path(self) -> None:
        """Preflight 5xx (transient) → run is NOT aborted by the preflight guard.

        A transient upstream error must not manufacture a new class of dispatch
        failures. The 5xx re-raises from the preflight check and is caught by
        the existing outer exception handler — the run may still fail, but NOT
        because of the preflight abort path.

        Verified by:
        - No 'preflight' comment posted (the preflight abort path posts a comment;
          the outer except handler for non-manage mode does not).
        - run.error does NOT contain 'preflight'.
        """
        from unittest.mock import AsyncMock, patch

        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import get_engine
        from agent_gtd_dispatch.main import _dispatch_worker
        from agent_gtd_dispatch.models import Run, RunStatus

        await db.init_db()
        run = Run(
            item_id="item-pf-500",
            project_name="TestProject",
            branch_name="feat/item-pf-500",
            engine="claude-code",
            callback_token="eyJ-run-token",
        )
        await db.insert_run(run)

        post_comment_mock = AsyncMock()

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
            patch("agent_gtd_dispatch.main.dispatch"),
        ):
            mock_client.get_item = AsyncMock(side_effect=_make_http_status_error(500))
            mock_client.is_authoritative_item_error = lambda exc: (
                exc.response.status_code in (401, 403, 404)
            )
            mock_client.post_comment = post_comment_mock

            engine = get_engine("claude-code")
            await _dispatch_worker(run, 50, engine, 1800)

        # Run ends up failed — but from the outer except, not the preflight path
        final_run = await db.get_run(run.id)
        assert final_run is not None
        assert final_run.status == RunStatus.failed
        # The error must NOT say 'preflight' — this failure was NOT due to the guard
        assert "preflight" not in (final_run.error or "").lower()
        # No preflight comment was posted (outer except for non-manage mode is silent)
        preflight_comments = [
            call
            for call in post_comment_mock.call_args_list
            if "preflight" in str(call).lower()
        ]
        assert len(preflight_comments) == 0


class TestConfigureLogging:
    """The package logger must actually emit — uvicorn configures only its own.

    Regression guard for 2026-09-19: the service ran for months with no logging
    configuration at all, so every gate-install decision, post-run gate result
    and manage-recovery branch was invisible in the journal.
    """

    @staticmethod
    def _reset() -> None:
        pkg_logger = logging.getLogger("agent_gtd_dispatch")
        for handler in list(pkg_logger.handlers):
            pkg_logger.removeHandler(handler)
        pkg_logger.setLevel(logging.NOTSET)
        pkg_logger.propagate = True

    def test_info_records_are_emitted_with_logger_name(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._reset()
        try:
            main.configure_logging("INFO")
            logging.getLogger("agent_gtd_dispatch.gates").info(
                "gate: decision=%s", "ok"
            )
            out = capsys.readouterr().out
        finally:
            self._reset()

        assert "gate: decision=ok" in out
        # The logger name is what lets you tell a gate line from a worker line.
        assert "agent_gtd_dispatch.gates" in out
        assert "INFO" in out

    def test_repeated_calls_do_not_stack_handlers(self) -> None:
        self._reset()
        try:
            main.configure_logging("INFO")
            main.configure_logging("INFO")
            main.configure_logging("DEBUG")
            owned = [
                h
                for h in logging.getLogger("agent_gtd_dispatch").handlers
                if getattr(h, "name", None) == main._LOG_HANDLER_NAME
            ]
        finally:
            self._reset()

        assert len(owned) == 1, "each call must replace its handler, not add one"

    def test_unknown_level_falls_back_to_info_rather_than_silencing(self) -> None:
        self._reset()
        try:
            main.configure_logging("NOT_A_LEVEL")
            level = logging.getLogger("agent_gtd_dispatch").level
        finally:
            self._reset()

        assert level == logging.INFO

    def test_level_is_honoured(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._reset()
        try:
            main.configure_logging("WARNING")
            logging.getLogger("agent_gtd_dispatch.worker").info("quiet")
            logging.getLogger("agent_gtd_dispatch.worker").warning("loud")
            out = capsys.readouterr().out
        finally:
            self._reset()

        assert "quiet" not in out
        assert "loud" in out


class TestReviewModeIsNotExternallyDispatchable:
    """Reviewers are worker-internal — the wave loop supplies their envelope."""

    def test_review_mode_dispatch_is_rejected(self, client, auth_headers) -> None:
        resp = client.post(
            "/dispatch",
            json={"item_id": "item-1", "max_turns": 40, "mode": "review"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "not externally dispatchable" in resp.json()["detail"]


class TestOneLoopPerRollout:
    """A host refuses a second MANAGE loop for a rollout it already drives."""

    @staticmethod
    def _mock_upstream(mock_client) -> None:
        mock_client.get_rollout = AsyncMock(
            return_value={"id": "wr-dup", "project_id": "proj1", "status": "running"}
        )
        mock_client.get_project = AsyncMock(
            return_value={
                "id": "proj1",
                "name": "WaveProject",
                "git_origin": "git@git.example.com:repos/wave-project",
            }
        )

    @patch("agent_gtd_dispatch.main._start_rollout_loop")
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_live_loop_yields_409_and_starts_nothing(
        self,
        mock_client,
        mock_dispatch,
        mock_loop,
        client,
        auth_headers,
        monkeypatch,
        caplog,
    ) -> None:
        from agent_gtd_dispatch.models import Run

        self._mock_upstream(mock_client)
        live = Run(project_name="WaveProject", mode="manage", rollout_id="wr-dup")
        monkeypatch.setattr(main, "_rollout_loops", {"wr-dup": live})
        refused: set[str] = set()
        monkeypatch.setattr(main, "_refused_relaunch_runs", refused)

        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            resp = client.post(
                "/dispatch",
                json={"rollout_id": "wr-dup", "mode": "manage", "max_turns": 50},
                headers=auth_headers,
            )

        assert resp.status_code == 409
        assert "wr-dup" in resp.json()["detail"]
        assert live.id in resp.json()["detail"]
        mock_loop.assert_not_called()
        assert live.id in refused
        assert any(
            "relaunch-refused" in r.getMessage()
            and "via=dispatch-409" in r.getMessage()
            and "marked=true" in r.getMessage()
            for r in caplog.records
        )

    @patch("agent_gtd_dispatch.main._start_rollout_loop")
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_queued_manage_entry_yields_409(
        self,
        mock_client,
        mock_dispatch,
        mock_loop,
        client,
        auth_headers,
        monkeypatch,
        caplog,
    ) -> None:
        from agent_gtd_dispatch.engines import get_engine
        from agent_gtd_dispatch.models import Run

        self._mock_upstream(mock_client)
        queued = Run(project_name="WaveProject", mode="manage", rollout_id="wr-dup")
        queue = [
            main._PendingDispatch(
                run=queued,
                engine=get_engine("claude-code"),
                max_turns=50,
                timeout_seconds=60,
                attribution=None,
            )
        ]
        monkeypatch.setattr(main, "_pending_queue", queue)
        refused: set[str] = set()
        monkeypatch.setattr(main, "_refused_relaunch_runs", refused)

        with caplog.at_level(logging.INFO, logger="agent_gtd_dispatch.main"):
            resp = client.post(
                "/dispatch",
                json={"rollout_id": "wr-dup", "mode": "manage", "max_turns": 50},
                headers=auth_headers,
            )

        assert resp.status_code == 409
        assert queued.id in resp.json()["detail"]
        assert len(queue) == 1
        mock_loop.assert_not_called()
        assert refused == set()
        assert any(
            "relaunch-refused" in r.getMessage() and "marked=false" in r.getMessage()
            for r in caplog.records
        )

    @patch("agent_gtd_dispatch.main._start_rollout_loop", return_value=False)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_choke_point_refusal_cancels_the_row_and_yields_409(
        self, mock_client, mock_dispatch, mock_loop, client, auth_headers
    ) -> None:
        self._mock_upstream(mock_client)

        resp = client.post(
            "/dispatch",
            json={"rollout_id": "wr-dup", "mode": "manage", "max_turns": 50},
            headers=auth_headers,
        )

        assert resp.status_code == 409
        refused = mock_loop.call_args.args[0]
        row = client.get(f"/runs/{refused.id}", headers=auth_headers).json()
        assert row["status"] == "cancelled"
        assert "already driven" in row["error"]

    @patch("agent_gtd_dispatch.main._start_rollout_loop", return_value=True)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    def test_fresh_loop_starts_after_a_halted_loop_deregistered(
        self, mock_client, mock_dispatch, mock_loop, client, auth_headers, monkeypatch
    ) -> None:
        from agent_gtd_dispatch.models import Run

        self._mock_upstream(mock_client)
        old = Run(project_name="WaveProject", mode="manage", rollout_id="wr-dup")
        loops = {"wr-dup": old}
        monkeypatch.setattr(main, "_rollout_loops", loops)
        # The halted loop deregisters at its exit decision (see _drive_rollout).
        main._deregister_rollout_loop(old)

        resp = client.post(
            "/dispatch",
            json={"rollout_id": "wr-dup", "mode": "manage", "max_turns": 50},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        mock_loop.assert_called_once()


class TestEngineSlotAdmission:
    """Per-engine slot admission on POST /dispatch (BUILD mode only)."""

    @pytest.fixture
    def slots(self, monkeypatch):
        """Fresh holder state, stubbed gtd_client/dispatch, parked workers."""
        import asyncio

        from agent_gtd_dispatch import config

        monkeypatch.setattr(main, "_engine_slot_holders", {})
        monkeypatch.setattr(main, "_engine_slot_admitting", set())
        monkeypatch.setattr(main, "_active_processes", {})
        monkeypatch.setattr(main, "_pending_queue", [])
        monkeypatch.setattr(config, "ENGINE_SLOTS", {"talos-qwen": 1})
        client_mock = AsyncMock()
        client_mock.get_item = AsyncMock(
            return_value={"id": "abc123", "title": "T", "project_id": "proj1"}
        )
        client_mock.get_project = AsyncMock(
            return_value={
                "id": "proj1",
                "name": "TestProject",
                "git_origin": "git@git.example.com:repos/test",
                "gate_command": "true",
            }
        )
        monkeypatch.setattr(main, "gtd_client", client_mock)
        from unittest.mock import MagicMock

        disp = MagicMock()
        disp.branch_name_for_item.return_value = "feat/abc123-t"
        monkeypatch.setattr(main, "dispatch", disp)

        state = {"release": None}

        async def _worker(run, *_a, **_kw):
            if state["release"] is None:
                state["release"] = asyncio.Event()
            await state["release"].wait()

        monkeypatch.setattr(main, "_dispatch_worker", _worker)
        return state

    @staticmethod
    def _post(client, auth_headers, engine="talos-qwen", mode="build"):
        return client.post(
            "/dispatch",
            json={"item_id": "abc123", "max_turns": 5, "engine": engine, "mode": mode},
            headers=auth_headers,
        )

    def test_first_admitted_second_refused(
        self, client, auth_headers, slots, caplog
    ) -> None:
        import asyncio

        from agent_gtd_dispatch import db

        caplog.set_level(logging.INFO)
        first = self._post(client, auth_headers)
        assert first.status_code == 200
        second = self._post(client, auth_headers)
        assert second.status_code == 503
        assert second.json()["detail"] == {
            "detail": "Engine slot full",
            "reason": "engine_slot_full",
            "engine": "talos-qwen",
            "active_runs": 1,
            "max_concurrent_runs": 1,
        }
        runs = client.portal.call(db.list_runs)
        assert [r.id for r in runs] == [first.json()["id"]]
        assert "engine-slot refuse" in caplog.text
        assert "engine=talos-qwen held=1 cap=1" in caplog.text
        assert "engine-slot admit" in caplog.text
        client.portal.call(asyncio.sleep, 0)

    def test_atomic_across_insert_run_await(
        self, client, auth_headers, slots, monkeypatch
    ) -> None:
        import asyncio

        from agent_gtd_dispatch import db

        real_insert = db.insert_run

        async def run_it() -> tuple[int, int]:
            entered = asyncio.Event()
            go = asyncio.Event()

            async def slow_insert(run):
                entered.set()
                await go.wait()
                await real_insert(run)

            monkeypatch.setattr(db, "insert_run", slow_insert)
            payload = {
                "item_id": "abc123",
                "max_turns": 5,
                "engine": "talos-qwen",
                "mode": "build",
            }
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                t1 = asyncio.create_task(
                    c.post("/dispatch", json=payload, headers=auth_headers)
                )
                await entered.wait()
                r2 = await c.post("/dispatch", json=payload, headers=auth_headers)
                go.set()
                r1 = await t1
            return r1.status_code, r2.status_code

        r1, r2 = client.portal.call(run_it)
        assert (r1, r2) == (200, 503)

    def test_reserve_and_release_unit(self, slots) -> None:
        assert main._try_reserve_engine_slot("talos-qwen", "a") is None
        assert main._try_reserve_engine_slot("talos-qwen", "b") == (1, 1)
        main._release_engine_slot("a")
        main._release_engine_slot("a")  # idempotent
        assert main._try_reserve_engine_slot("talos-qwen", "b") is None
        assert main._try_reserve_engine_slot("claude-code", "c") is None

    def test_no_caps_admits_as_before(
        self, client, auth_headers, slots, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config

        monkeypatch.setattr(config, "ENGINE_SLOTS", {})
        assert self._post(client, auth_headers).status_code == 200

    def test_cap_zero_refuses(self, client, auth_headers, slots, monkeypatch) -> None:
        from agent_gtd_dispatch import config

        monkeypatch.setattr(config, "ENGINE_SLOTS", {"talos-qwen": 0})
        resp = self._post(client, auth_headers)
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["reason"] == "engine_slot_full"
        assert detail["active_runs"] == 0
        assert detail["max_concurrent_runs"] == 0

    def test_other_engine_unaffected(self, client, auth_headers, slots) -> None:
        assert self._post(client, auth_headers).status_code == 200
        assert (
            self._post(client, auth_headers, engine="claude-code-sonnet").status_code
            == 200
        )

    def test_non_build_not_capped(
        self, client, auth_headers, slots, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config

        monkeypatch.setattr(config, "ENGINE_SLOTS", {"claude-code": 0})
        resp = self._post(client, auth_headers, engine="claude-code", mode="plan")
        assert resp.status_code != 503 or "engine_slot_full" not in resp.text

    def test_release_on_completion(self, client, auth_headers, slots) -> None:
        import asyncio

        assert self._post(client, auth_headers).status_code == 200
        assert main._engine_slot_holders["talos-qwen"]

        async def finish() -> None:
            slots["release"].set()
            await asyncio.sleep(0.05)

        client.portal.call(finish)
        assert main._engine_slot_holders.get("talos-qwen", set()) == set()

    def test_release_on_early_cancel(self, client, auth_headers, slots) -> None:
        import asyncio

        assert self._post(client, auth_headers).status_code == 200
        (task,) = main._active_processes.values()

        async def cancel() -> None:
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        client.portal.call(cancel)
        assert main._engine_slot_holders.get("talos-qwen", set()) == set()

    def test_release_on_insert_failure(
        self, client, auth_headers, slots, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import db

        async def boom(_run):
            raise RuntimeError("db down")

        monkeypatch.setattr(db, "insert_run", boom)
        with pytest.raises(RuntimeError):
            self._post(client, auth_headers)
        assert main._engine_slot_holders.get("talos-qwen", set()) == set()


class TestEnginesOnlyAdmission:
    """DISPATCH_ENGINES_ONLY admission on POST /dispatch and /info."""

    @pytest.fixture
    def slots(self, monkeypatch):
        import asyncio

        from agent_gtd_dispatch import config

        monkeypatch.setattr(main, "_engine_slot_holders", {})
        monkeypatch.setattr(main, "_engine_slot_admitting", set())
        monkeypatch.setattr(main, "_active_processes", {})
        monkeypatch.setattr(main, "_pending_queue", [])
        monkeypatch.setattr(config, "ENGINE_SLOTS", {})
        monkeypatch.setattr(config, "ENGINES_ONLY", None)
        client_mock = AsyncMock()
        client_mock.get_item = AsyncMock(
            return_value={"id": "abc123", "title": "T", "project_id": "proj1"}
        )
        client_mock.get_project = AsyncMock(
            return_value={
                "id": "proj1",
                "name": "TestProject",
                "git_origin": "git@git.example.com:repos/test",
                "gate_command": "true",
            }
        )
        client_mock.get_rollout = AsyncMock()
        monkeypatch.setattr(main, "gtd_client", client_mock)
        from unittest.mock import MagicMock

        disp = MagicMock()
        disp.branch_name_for_item.return_value = "feat/abc123-t"
        monkeypatch.setattr(main, "dispatch", disp)

        state = {"release": None, "client": client_mock}

        async def _worker(run, *_a, **_kw):
            if state["release"] is None:
                state["release"] = asyncio.Event()
            await state["release"].wait()

        monkeypatch.setattr(main, "_dispatch_worker", _worker)
        return state

    @staticmethod
    def _post(client, auth_headers, engine="talos-qwen", mode="build"):
        body = {"item_id": "abc123", "max_turns": 5, "engine": engine, "mode": mode}
        if mode == "manage":
            body = {
                "rollout_id": "r1",
                "max_turns": 5,
                "engine": engine,
                "mode": "manage",
            }
        return client.post("/dispatch", json=body, headers=auth_headers)

    @staticmethod
    def _only(monkeypatch, names):
        from agent_gtd_dispatch import config

        monkeypatch.setattr(
            config, "ENGINES_ONLY", None if names is None else frozenset(names)
        )

    def test_refused_build(
        self, client, auth_headers, slots, monkeypatch, caplog
    ) -> None:
        caplog.set_level(logging.INFO)
        self._only(monkeypatch, {"talos-qwen"})
        r = self._post(client, auth_headers, "claude-code-sonnet")
        assert r.status_code == 422
        assert r.json()["detail"] == {
            "detail": "Engine not served by this host",
            "reason": "engine_not_allowed",
            "engine": "claude-code-sonnet",
            "allowed_engines": ["talos-qwen"],
        }
        assert "engines-only refuse engine=claude-code-sonnet" in caplog.text
        assert "allowed=['talos-qwen']" in caplog.text

    @pytest.mark.parametrize(
        ("engine", "mode"), [("claude-code", "plan"), ("claude-code", "manage")]
    )
    def test_refused_plan_manage(
        self, client, auth_headers, slots, monkeypatch, engine, mode
    ) -> None:
        self._only(monkeypatch, {"talos-qwen"})
        r = self._post(client, auth_headers, engine, mode)
        assert r.status_code == 422
        assert r.json()["detail"]["reason"] == "engine_not_allowed"
        slots["client"].get_item.assert_not_awaited()
        slots["client"].get_rollout.assert_not_awaited()

    def test_listed_admitted(self, client, auth_headers, slots, monkeypatch) -> None:
        self._only(monkeypatch, {"talos-qwen"})
        assert self._post(client, auth_headers).status_code == 200

    def test_requested_engine_checked_not_swapped(
        self, client, auth_headers, slots, monkeypatch
    ) -> None:
        self._only(monkeypatch, {"claude-code-glm"})
        r = self._post(client, auth_headers, "claude-code-glm", "plan")
        assert r.status_code == 200
        self._only(monkeypatch, {"claude-code"})
        r = self._post(client, auth_headers, "claude-code-glm", "plan")
        assert r.status_code == 422
        d = r.json()["detail"]
        assert d["engine"] == "claude-code-glm"
        assert d["allowed_engines"] == ["claude-code"]

    def test_unknown_engine_is_400(
        self, client, auth_headers, slots, monkeypatch
    ) -> None:
        self._only(monkeypatch, {"talos-qwen"})
        assert self._post(client, auth_headers, "nope").status_code == 400

    def test_unset_unchanged(self, client, auth_headers, slots) -> None:
        assert self._post(client, auth_headers, "claude-code-sonnet").status_code == 200

    def test_refusal_has_no_side_effects(
        self, client, auth_headers, slots, monkeypatch
    ) -> None:
        from agent_gtd_dispatch import config, db

        monkeypatch.setattr(config, "ENGINE_SLOTS", {"claude-code-sonnet": 1})
        self._only(monkeypatch, {"talos-qwen"})
        before = len(client.portal.call(db.list_runs))
        r = self._post(client, auth_headers, "claude-code-sonnet")
        assert r.status_code == 422
        assert main._engine_slot_holders == {}
        assert len(client.portal.call(db.list_runs)) == before

    def test_talos_non_build_check_first(
        self, client, auth_headers, slots, monkeypatch
    ) -> None:
        self._only(monkeypatch, {"claude-code"})
        r = self._post(client, auth_headers, "talos-qwen", "plan")
        assert r.status_code == 422
        assert r.json()["detail"].startswith("Engine 'talos-qwen' is a talos engine")

    def test_review_refused(self, client, auth_headers, slots, monkeypatch) -> None:
        self._only(monkeypatch, {"talos-qwen"})
        r = self._post(client, auth_headers, "claude-code", "review")
        assert r.status_code == 422
        assert r.json()["detail"]["reason"] == "engine_not_allowed"

    def test_info(self, client, slots, monkeypatch) -> None:
        from agent_gtd_dispatch import config

        self._only(monkeypatch, {"talos-qwen"})
        monkeypatch.setattr(config, "ENGINE_SLOTS", {"talos-qwen": 1, "claude-code": 2})
        monkeypatch.setattr(config, "TALOS_QWEN_BASE_URL", "http://127.0.0.1:11434")
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
        monkeypatch.setattr(config, "OLLAMA_BASE_URL", "")
        monkeypatch.setattr(config, "OLLAMA_CLOUD_API_KEY", "")
        monkeypatch.delenv("KIRO_API_KEY", raising=False)
        info = client.get("/info").json()
        assert info["engines"] == ["talos-qwen"]
        assert info["engines_only"] == ["talos-qwen"]
        assert set(info["engine_slots"]) == {"talos-qwen", "claude-code"}
        self._only(monkeypatch, None)
        assert client.get("/info").json()["engines_only"] is None
