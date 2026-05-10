"""Tests for the GTD HTTP client."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


@pytest.fixture(autouse=True)
def _env(tmp_path):
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


def _make_client_mock(
    json_data: dict | None = None,
    content: bytes = b"...",
) -> tuple[AsyncMock, MagicMock]:
    """Return a (mock_client, mock_response) pair for httpx.AsyncClient."""
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.content = content
    mock_response.raise_for_status = MagicMock()
    if json_data is not None:
        mock_response.json.return_value = json_data
    mock_client.request.return_value = mock_response
    return mock_client, mock_response


class TestGetItem:
    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_returns_json(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import get_item

        mock_client, _ = _make_client_mock({"id": "item1", "title": "My Task"})
        mock_cls.return_value.__aenter__.return_value = mock_client

        result = await get_item("item1")

        assert result == {"id": "item1", "title": "My Task"}

    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_url_and_auth_header(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import get_item

        mock_client, _ = _make_client_mock({"id": "item1"})
        mock_cls.return_value.__aenter__.return_value = mock_client

        await get_item("item1")

        mock_client.request.assert_called_once_with(
            "GET",
            "http://localhost:9999/api/items/item1",
            headers={"Authorization": "Bearer test-gtd-key"},
        )


class TestGetProject:
    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_returns_json(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import get_project

        mock_client, _ = _make_client_mock({"id": "proj1", "name": "My Project"})
        mock_cls.return_value.__aenter__.return_value = mock_client

        result = await get_project("proj1")

        assert result == {"id": "proj1", "name": "My Project"}

    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_url_and_auth_header(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import get_project

        mock_client, _ = _make_client_mock({"id": "proj1"})
        mock_cls.return_value.__aenter__.return_value = mock_client

        await get_project("proj1")

        mock_client.request.assert_called_once_with(
            "GET",
            "http://localhost:9999/api/projects/proj1",
            headers={"Authorization": "Bearer test-gtd-key"},
        )


class TestPostComment:
    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_posts_to_correct_url_with_body(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import post_comment

        mock_client, _ = _make_client_mock(content=b"")
        mock_cls.return_value.__aenter__.return_value = mock_client

        result = await post_comment(
            "item1", "Hello world", created_by="claude-dispatch"
        )

        assert result is None
        mock_client.request.assert_called_once_with(
            "POST",
            "http://localhost:9999/api/items/item1/comments",
            headers={"Authorization": "Bearer test-gtd-key"},
            json={"content_markdown": "Hello world", "created_by": "claude-dispatch"},
        )

    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_kiro_created_by_propagates(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import post_comment

        mock_client, _ = _make_client_mock(content=b"")
        mock_cls.return_value.__aenter__.return_value = mock_client

        await post_comment("item1", "Hello world", created_by="kiro-dispatch")

        mock_client.request.assert_called_once_with(
            "POST",
            "http://localhost:9999/api/items/item1/comments",
            headers={"Authorization": "Bearer test-gtd-key"},
            json={"content_markdown": "Hello world", "created_by": "kiro-dispatch"},
        )

    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_empty_response_returns_none(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import post_comment

        mock_client, _ = _make_client_mock(content=b"")
        mock_cls.return_value.__aenter__.return_value = mock_client

        result = await post_comment(
            "item1", "Test comment", created_by="claude-dispatch"
        )

        assert result is None


class TestRequestErrorHandling:
    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_404_propagates(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import get_item

        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = b"Not Found"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404 Not Found",
            request=MagicMock(),
            response=MagicMock(),
        )
        mock_client.request.return_value = mock_response

        with pytest.raises(httpx.HTTPStatusError):
            await get_item("missing")

    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_500_propagates(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import get_project

        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = b"Internal Server Error"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=MagicMock(),
            response=MagicMock(),
        )
        mock_client.request.return_value = mock_response

        with pytest.raises(httpx.HTTPStatusError):
            await get_project("proj1")


class TestRequestEmptyBody:
    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_empty_content_returns_empty_dict(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import get_item

        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = b""
        mock_response.raise_for_status = MagicMock()
        mock_client.request.return_value = mock_response

        result = await get_item("abc")

        assert result == {}


class TestListAttachments:
    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_returns_list(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import list_attachments

        attachments = [
            {"id": "att-1", "filename": "spec.md", "size_bytes": 1024},
            {"id": "att-2", "filename": "photo.png", "size_bytes": 8192},
        ]
        mock_client, _ = _make_client_mock(json_data=attachments)  # type: ignore[arg-type]
        mock_cls.return_value.__aenter__.return_value = mock_client

        result = await list_attachments("item1")

        assert result == attachments

    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_url_and_auth_header(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import list_attachments

        mock_client, _ = _make_client_mock(json_data=[])
        mock_cls.return_value.__aenter__.return_value = mock_client

        await list_attachments("item1")

        mock_client.request.assert_called_once_with(
            "GET",
            "http://localhost:9999/api/items/item1/attachments",
            headers={"Authorization": "Bearer test-gtd-key"},
        )

    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_error_propagates(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import list_attachments

        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = b"Not Found"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404 Not Found", request=MagicMock(), response=MagicMock()
        )
        mock_client.request.return_value = mock_response

        with pytest.raises(httpx.HTTPStatusError):
            await list_attachments("missing-item")


class TestDownloadAttachment:
    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_returns_raw_bytes(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import download_attachment

        file_bytes = b"\x89PNG\r\n\x1a\n..."
        mock_client, _ = _make_client_mock(content=file_bytes)
        mock_cls.return_value.__aenter__.return_value = mock_client

        result = await download_attachment("att-1")

        assert result == file_bytes

    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_url_and_auth_header(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import download_attachment

        mock_client, _ = _make_client_mock(content=b"bytes")
        mock_cls.return_value.__aenter__.return_value = mock_client

        await download_attachment("att-99")

        mock_client.request.assert_called_once_with(
            "GET",
            "http://localhost:9999/api/attachments/att-99",
            headers={"Authorization": "Bearer test-gtd-key"},
        )

    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_error_propagates(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import download_attachment

        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = b"Not Found"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404 Not Found", request=MagicMock(), response=MagicMock()
        )
        mock_client.request.return_value = mock_response

        with pytest.raises(httpx.HTTPStatusError):
            await download_attachment("missing-att")


class TestWaveRunMethods:
    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_advance_wave_calls_correct_endpoint(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import advance_wave

        payload = {"next_ready": ["item-1"], "in_progress": [], "graph_complete": False}
        mock_client, _ = _make_client_mock(json_data=payload)
        mock_cls.return_value.__aenter__.return_value = mock_client

        result = await advance_wave("wr-123")

        assert result == payload
        mock_client.request.assert_called_once_with(
            "POST",
            "http://localhost:9999/api/wave_runs/wr-123/advance",
            headers={"Authorization": "Bearer test-gtd-key"},
        )

    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_advance_wave_raises_on_http_error(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import advance_wave

        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = b"Internal Server Error"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=MagicMock(),
            response=MagicMock(),
        )
        mock_client.request.return_value = mock_response

        with pytest.raises(httpx.HTTPStatusError):
            await advance_wave("wr-123")

    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_complete_in_wave_sends_merge_actor(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import complete_in_wave

        mock_client, _ = _make_client_mock(content=b"")
        mock_cls.return_value.__aenter__.return_value = mock_client

        await complete_in_wave(
            "wr-123",
            "item-1",
            outcome="completed",
            merge_actor="manager-allowlist",
            decision_rule="safe-docs",
        )

        mock_client.request.assert_called_once_with(
            "POST",
            "http://localhost:9999/api/wave_runs/wr-123/complete_item",
            headers={"Authorization": "Bearer test-gtd-key"},
            json={
                "item_id": "item-1",
                "outcome": "completed",
                "merge_actor": "manager-allowlist",
                "decision_rule": "safe-docs",
            },
        )

    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_halt_wave_sends_reason(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import halt_wave

        mock_client, _ = _make_client_mock(content=b"")
        mock_cls.return_value.__aenter__.return_value = mock_client

        await halt_wave("wr-123", reason="CI failure on feat/x")

        mock_client.request.assert_called_once_with(
            "POST",
            "http://localhost:9999/api/wave_runs/wr-123/halt",
            headers={"Authorization": "Bearer test-gtd-key"},
            json={"reason": "CI failure on feat/x"},
        )

    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_list_comments_returns_list(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import list_comments

        comments = [
            {"id": "c-1", "content_markdown": "Done!", "created_by": "claude-dispatch"},
            {"id": "c-2", "content_markdown": "LGTM", "created_by": "mcp-agent"},
        ]
        mock_client, _ = _make_client_mock(json_data=comments)  # type: ignore[arg-type]
        mock_cls.return_value.__aenter__.return_value = mock_client

        result = await list_comments("item-1")

        assert result == comments
        mock_client.request.assert_called_once_with(
            "GET",
            "http://localhost:9999/api/items/item-1/comments",
            headers={"Authorization": "Bearer test-gtd-key"},
        )
