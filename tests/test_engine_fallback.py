"""Tests for engine fallback/swap visibility (AC-5a through AC-5d)."""

from __future__ import annotations

import os
import subprocess
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


class TestPlanModeEngineSwap:
    """AC-5a: plan-mode POST /dispatch with engine=claude-code-ollama."""

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    async def test_plan_mode_ollama_response_fields(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers, tmp_path
    ) -> None:
        from agent_gtd_dispatch import db

        await db.init_db()

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

        # engine = requested (preserved), engine_actual = effective (rewritten)
        assert data["engine"] == "claude-code-ollama"
        assert data["engine_actual"] == "claude"
        assert data["engine_swap"] is not None
        assert data["engine_swap"]["from_engine"] == "claude-code-ollama"
        assert data["engine_swap"]["to_engine"] == "claude"
        assert "plan/manage" in data["engine_swap"]["reason"]

        # DB row must also have both fields set correctly
        run = await db.get_run(data["id"])
        assert run is not None
        assert run.engine == "claude-code-ollama"
        assert run.engine_actual == "claude"

    @patch("agent_gtd_dispatch.main._dispatch_worker", new_callable=AsyncMock)
    @patch("agent_gtd_dispatch.main.dispatch")
    @patch("agent_gtd_dispatch.main.gtd_client")
    async def test_build_mode_ollama_no_swap(
        self, mock_client, mock_dispatch, mock_worker, client, auth_headers, tmp_path
    ) -> None:
        """Build mode + ollama: no swap, engine_swap is None."""
        from agent_gtd_dispatch import db

        await db.init_db()

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
                "mode": "build",
            },
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["engine"] == "claude-code-ollama"
        assert data["engine_actual"] == "claude-code-ollama"
        assert data["engine_swap"] is None

        run = await db.get_run(data["id"])
        assert run is not None
        assert run.engine == "claude-code-ollama"
        assert run.engine_actual == "claude-code-ollama"


class TestOllamaFallbackDbUpdate:
    """AC-5b: _dispatch_worker persists engine_actual and error on Ollama fallback."""

    async def test_fallback_updates_db_engine_actual_and_error(self, tmp_path) -> None:
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
            engine_actual="claude-code-ollama",
        )
        await db.insert_run(run)

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch(
                "agent_gtd_dispatch.main._ollama_health_check",
                new_callable=AsyncMock,
                return_value=(False, "connection refused"),
            ),
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_client.post_comment = AsyncMock()
            mock_client.get_item = AsyncMock(
                return_value={
                    "id": "item-fallback",
                    "title": "Fix bug",
                    "project_id": "proj1",
                }
            )
            mock_client.get_project = AsyncMock(
                return_value={
                    "id": "proj1",
                    "name": "TestProject",
                    "git_origin": "git@host:repos/test",
                }
            )
            mock_dispatch.prepare_workspace.return_value = tmp_path
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt.return_value = "system prompt"
            mock_dispatch.run_agent = AsyncMock(return_value=mock_result)
            mock_dispatch.cleanup_workspace = MagicMock()

            await _dispatch_worker(run, 50, CLAUDE_OLLAMA, 1800)

        updated = await db.get_run(run.id)
        assert updated is not None
        assert updated.engine_actual == "claude"
        assert updated.error == "ollama_fallback: connection refused"


class TestOllamaFallbackCommentFailure:
    """AC-5c: comment post failure does not clobber DB fallback signal."""

    async def test_comment_failure_db_still_has_fallback_signal(self, tmp_path) -> None:
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
            engine_actual="claude-code-ollama",
        )
        await db.insert_run(run)

        mock_result = MagicMock()
        mock_result.returncode = 0

        call_count = 0

        async def first_call_raises(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("network error posting fallback comment")

        with (
            patch(
                "agent_gtd_dispatch.main._ollama_health_check",
                new_callable=AsyncMock,
                return_value=(False, "connection refused"),
            ),
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_client.post_comment = AsyncMock(side_effect=first_call_raises)
            mock_client.get_item = AsyncMock(
                return_value={
                    "id": "item-fallback",
                    "title": "Fix bug",
                    "project_id": "proj1",
                }
            )
            mock_client.get_project = AsyncMock(
                return_value={
                    "id": "proj1",
                    "name": "TestProject",
                    "git_origin": "git@host:repos/test",
                }
            )
            mock_dispatch.prepare_workspace.return_value = tmp_path
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt.return_value = "system prompt"
            mock_dispatch.run_agent = AsyncMock(return_value=mock_result)
            mock_dispatch.cleanup_workspace = MagicMock()

            await _dispatch_worker(run, 50, CLAUDE_OLLAMA, 1800)

        updated = await db.get_run(run.id)
        assert updated is not None
        # engine_actual is set by the DB update that precedes the comment post
        assert updated.engine_actual == "claude"
        # The error prefix is preserved; it may have been written before the failed comment
        assert updated.error is not None
        assert updated.error.startswith("ollama_fallback:")


class TestAttributionVocabulary:
    """AC-5d: all four comment-posting paths use attribution or 'agent-gtd-dispatch'."""

    async def test_fallback_comment_uses_attribution(self, tmp_path) -> None:
        """Ollama fallback comment uses provided attribution."""
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import CLAUDE_OLLAMA
        from agent_gtd_dispatch.main import _dispatch_worker
        from agent_gtd_dispatch.models import Run

        await db.init_db()
        run = Run(
            item_id="item-attr",
            project_name="TestProject",
            branch_name="feat/abc-fix",
            engine="claude-code-ollama",
            engine_actual="claude-code-ollama",
        )
        await db.insert_run(run)

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch(
                "agent_gtd_dispatch.main._ollama_health_check",
                new_callable=AsyncMock,
                return_value=(False, "connection refused"),
            ),
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_client.post_comment = AsyncMock()
            mock_client.get_item = AsyncMock(
                return_value={
                    "id": "item-attr",
                    "title": "Fix bug",
                    "project_id": "proj1",
                }
            )
            mock_client.get_project = AsyncMock(
                return_value={
                    "id": "proj1",
                    "name": "TestProject",
                    "git_origin": "git@host:repos/test",
                }
            )
            mock_dispatch.prepare_workspace.return_value = tmp_path
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt.return_value = "system prompt"
            mock_dispatch.run_agent = AsyncMock(return_value=mock_result)
            mock_dispatch.cleanup_workspace = MagicMock()

            await _dispatch_worker(run, 50, CLAUDE_OLLAMA, 1800, attribution="my-agent")

        # The first post_comment call is the fallback comment
        fallback_call = mock_client.post_comment.call_args_list[0]
        assert fallback_call.kwargs["created_by"] == "my-agent"

    async def test_dispatch_comment_uses_attribution(self, tmp_path) -> None:
        """Dispatch start comment uses provided attribution."""
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import CLAUDE
        from agent_gtd_dispatch.main import _dispatch_worker
        from agent_gtd_dispatch.models import Run

        await db.init_db()
        run = Run(
            item_id="item-attr",
            project_name="TestProject",
            branch_name="feat/abc-fix",
            engine="claude",
            engine_actual="claude",
        )
        await db.insert_run(run)

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_client.post_comment = AsyncMock()
            mock_client.get_item = AsyncMock(
                return_value={
                    "id": "item-attr",
                    "title": "Fix bug",
                    "project_id": "proj1",
                }
            )
            mock_client.get_project = AsyncMock(
                return_value={
                    "id": "proj1",
                    "name": "TestProject",
                    "git_origin": "git@host:repos/test",
                }
            )
            mock_dispatch.prepare_workspace.return_value = tmp_path
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt.return_value = "system prompt"
            mock_dispatch.run_agent = AsyncMock(return_value=mock_result)
            mock_dispatch.cleanup_workspace = MagicMock()

            await _dispatch_worker(run, 50, CLAUDE, 1800, attribution="my-agent")

        # The first post_comment call is the dispatch comment
        dispatch_call = mock_client.post_comment.call_args_list[0]
        assert dispatch_call.kwargs["created_by"] == "my-agent"

    async def test_error_exit_comment_uses_attribution(self, tmp_path) -> None:
        """Error exit-code comment uses provided attribution."""
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import CLAUDE
        from agent_gtd_dispatch.main import _dispatch_worker
        from agent_gtd_dispatch.models import Run

        await db.init_db()
        run = Run(
            item_id="item-attr",
            project_name="TestProject",
            branch_name="feat/abc-fix",
            engine="claude",
            engine_actual="claude",
        )
        await db.insert_run(run)

        # Write a transcript file so error_msg is non-None (triggers the comment)
        transcript = tmp_path / "transcript.txt"
        transcript.write_text("Agent error output here")

        mock_result = MagicMock()
        mock_result.returncode = 1

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_client.post_comment = AsyncMock()
            mock_client.get_item = AsyncMock(
                return_value={
                    "id": "item-attr",
                    "title": "Fix bug",
                    "project_id": "proj1",
                }
            )
            mock_client.get_project = AsyncMock(
                return_value={
                    "id": "proj1",
                    "name": "TestProject",
                    "git_origin": "git@host:repos/test",
                }
            )
            mock_dispatch.prepare_workspace.return_value = tmp_path
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt.return_value = "system prompt"
            mock_dispatch.run_agent = AsyncMock(return_value=mock_result)
            mock_dispatch.cleanup_workspace = MagicMock()

            await _dispatch_worker(run, 50, CLAUDE, 1800, attribution="my-agent")

        # Two calls: dispatch comment, then error comment
        assert mock_client.post_comment.call_count == 2
        error_call = mock_client.post_comment.call_args_list[1]
        assert error_call.kwargs["created_by"] == "my-agent"

    async def test_timeout_comment_uses_attribution(self, tmp_path) -> None:
        """Timeout comment uses provided attribution."""
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import CLAUDE
        from agent_gtd_dispatch.main import _dispatch_worker
        from agent_gtd_dispatch.models import Run

        await db.init_db()
        run = Run(
            item_id="item-attr",
            project_name="TestProject",
            branch_name="feat/abc-fix",
            engine="claude",
            engine_actual="claude",
        )
        await db.insert_run(run)

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_client.post_comment = AsyncMock()
            mock_client.get_item = AsyncMock(
                return_value={
                    "id": "item-attr",
                    "title": "Fix bug",
                    "project_id": "proj1",
                }
            )
            mock_client.get_project = AsyncMock(
                return_value={
                    "id": "proj1",
                    "name": "TestProject",
                    "git_origin": "git@host:repos/test",
                }
            )
            mock_dispatch.prepare_workspace.return_value = tmp_path
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt.return_value = "system prompt"
            mock_dispatch.run_agent = AsyncMock(
                side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1800)
            )
            mock_dispatch.cleanup_workspace = MagicMock()

            await _dispatch_worker(run, 50, CLAUDE, 1800, attribution="my-agent")

        # Two calls: dispatch comment, then timeout comment
        assert mock_client.post_comment.call_count == 2
        timeout_call = mock_client.post_comment.call_args_list[1]
        assert timeout_call.kwargs["created_by"] == "my-agent"

    async def test_default_attribution_when_none(self, tmp_path) -> None:
        """When attribution=None, fallback to 'agent-gtd-dispatch'."""
        from agent_gtd_dispatch import db
        from agent_gtd_dispatch.engines import CLAUDE
        from agent_gtd_dispatch.main import _dispatch_worker
        from agent_gtd_dispatch.models import Run

        await db.init_db()
        run = Run(
            item_id="item-noattr",
            project_name="TestProject",
            branch_name="feat/abc-fix",
            engine="claude",
            engine_actual="claude",
        )
        await db.insert_run(run)

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch("agent_gtd_dispatch.main.gtd_client") as mock_client,
            patch("agent_gtd_dispatch.main.dispatch") as mock_dispatch,
        ):
            mock_client.post_comment = AsyncMock()
            mock_client.get_item = AsyncMock(
                return_value={
                    "id": "item-noattr",
                    "title": "Fix bug",
                    "project_id": "proj1",
                }
            )
            mock_client.get_project = AsyncMock(
                return_value={
                    "id": "proj1",
                    "name": "TestProject",
                    "git_origin": "git@host:repos/test",
                }
            )
            mock_dispatch.prepare_workspace.return_value = tmp_path
            mock_dispatch.stage_attachments = AsyncMock(return_value=[])
            mock_dispatch.build_system_prompt.return_value = "system prompt"
            mock_dispatch.run_agent = AsyncMock(return_value=mock_result)
            mock_dispatch.cleanup_workspace = MagicMock()

            # attribution=None (default)
            await _dispatch_worker(run, 50, CLAUDE, 1800)

        # Dispatch comment should use the default
        dispatch_call = mock_client.post_comment.call_args_list[0]
        assert dispatch_call.kwargs["created_by"] == "agent-gtd-dispatch"
