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
        "ANTHROPIC_API_KEY": "test-anthropic-key",
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
            return_value={"id": "abc12345-6789", "title": "Fix bug", "project_id": "proj1"}
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
        assert data["id"]  # has a run ID


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
