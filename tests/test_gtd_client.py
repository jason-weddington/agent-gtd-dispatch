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

        result = await post_comment("item1", "Hello world")

        assert result is None
        mock_client.request.assert_called_once_with(
            "POST",
            "http://localhost:9999/api/items/item1/comments",
            headers={"Authorization": "Bearer test-gtd-key"},
            json={"content_markdown": "Hello world", "created_by": "claude-dispatch"},
        )

    @patch("agent_gtd_dispatch.gtd_client.httpx.AsyncClient")
    async def test_empty_response_returns_none(self, mock_cls) -> None:
        from agent_gtd_dispatch.gtd_client import post_comment

        mock_client, _ = _make_client_mock(content=b"")
        mock_cls.return_value.__aenter__.return_value = mock_client

        result = await post_comment("item1", "Test comment")

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
