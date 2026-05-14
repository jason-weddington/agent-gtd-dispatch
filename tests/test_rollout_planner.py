"""Tests for rollout_planner module."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(tmp_path):
    """Set required env vars including ANTHROPIC_API_KEY."""
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


def _make_tool_response(edges: list[dict[str, str]]) -> MagicMock:
    """Build a mock anthropic response with a tool_use block."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = {"edges": edges}
    response = MagicMock()
    response.content = [tool_block]
    return response


def _make_gtd_client(items: list[dict[str, Any]]) -> MagicMock:
    """Build a mock gtd_client whose get_item returns items in order."""
    mock = MagicMock()
    mock.get_item = AsyncMock(side_effect=items)
    return mock


def _make_anthropic_module(mock_response: MagicMock) -> MagicMock:
    """Build a mock anthropic module returning mock_response from messages.create."""
    mock_api_client = MagicMock()
    mock_api_client.messages.create = AsyncMock(return_value=mock_response)
    mock_module = MagicMock()
    mock_module.AsyncAnthropic.return_value = mock_api_client
    return mock_module


class TestPlanRollout:
    async def test_two_items_with_blocker_returns_edge(self) -> None:
        items = [
            {"id": "id1", "title": "Item 1", "description": "", "blockers": []},
            {"id": "id2", "title": "Item 2", "description": "", "blockers": ["id1"]},
        ]
        mock_response = _make_tool_response(
            [{"from_item_id": "id1", "to_item_id": "id2"}]
        )
        mock_gtd = _make_gtd_client(items)
        mock_anthropic = _make_anthropic_module(mock_response)

        with (
            patch("agent_gtd_dispatch.rollout_planner.gtd_client", mock_gtd),
            patch("agent_gtd_dispatch.rollout_planner.anthropic", mock_anthropic),
        ):
            from agent_gtd_dispatch.rollout_planner import plan_rollout

            result = await plan_rollout(["id1", "id2"])

        assert result.nodes == ["id1", "id2"]
        assert len(result.edges) == 1
        assert result.edges[0].from_item_id == "id1"
        assert result.edges[0].to_item_id == "id2"
        assert result.planner_model == "claude-sonnet-4-6"

    async def test_two_unrelated_items_no_edges(self) -> None:
        items = [
            {"id": "id1", "title": "Item 1", "description": "", "blockers": []},
            {"id": "id2", "title": "Item 2", "description": "", "blockers": []},
        ]
        mock_response = _make_tool_response([])
        mock_gtd = _make_gtd_client(items)
        mock_anthropic = _make_anthropic_module(mock_response)

        with (
            patch("agent_gtd_dispatch.rollout_planner.gtd_client", mock_gtd),
            patch("agent_gtd_dispatch.rollout_planner.anthropic", mock_anthropic),
        ):
            from agent_gtd_dispatch.rollout_planner import plan_rollout

            result = await plan_rollout(["id1", "id2"])

        assert sorted(result.nodes) == ["id1", "id2"]
        assert result.edges == []

    async def test_llm_unknown_id_filtered(self) -> None:
        items = [
            {"id": "id1", "title": "Item 1", "description": "", "blockers": []},
            {"id": "id2", "title": "Item 2", "description": "", "blockers": []},
        ]
        mock_response = _make_tool_response(
            [{"from_item_id": "id1", "to_item_id": "unknown-xyz"}]
        )
        mock_gtd = _make_gtd_client(items)
        mock_anthropic = _make_anthropic_module(mock_response)

        with (
            patch("agent_gtd_dispatch.rollout_planner.gtd_client", mock_gtd),
            patch("agent_gtd_dispatch.rollout_planner.anthropic", mock_anthropic),
        ):
            from agent_gtd_dispatch.rollout_planner import plan_rollout

            result = await plan_rollout(["id1", "id2"])

        assert result.edges == []
        assert sorted(result.nodes) == ["id1", "id2"]

    async def test_gtd_client_failure_propagates(self) -> None:
        mock_gtd = MagicMock()
        mock_gtd.get_item = AsyncMock(side_effect=RuntimeError("GTD API down"))
        mock_anthropic = MagicMock()

        with (
            patch("agent_gtd_dispatch.rollout_planner.gtd_client", mock_gtd),
            patch("agent_gtd_dispatch.rollout_planner.anthropic", mock_anthropic),
        ):
            from agent_gtd_dispatch.rollout_planner import plan_rollout

            with pytest.raises(RuntimeError, match="GTD API down"):
                await plan_rollout(["id1"])


class TestBuildContext:
    def test_formats_item_with_blockers(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _build_context

        items = [
            {
                "id": "id1",
                "title": "First Item",
                "description": "Do some work",
                "blockers": ["id0"],
            }
        ]
        ctx = _build_context(items)

        assert "## Item id1: First Item" in ctx
        assert "Blockers (must complete first): id0" in ctx
        assert "Do some work" in ctx

    def test_no_blockers_shows_none(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _build_context

        items = [{"id": "id1", "title": "T", "description": "", "blockers": []}]
        ctx = _build_context(items)

        assert "Blockers (must complete first): none" in ctx

    def test_multiple_blockers_joined(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _build_context

        items = [
            {
                "id": "id3",
                "title": "C",
                "description": "",
                "blockers": ["id1", "id2"],
            }
        ]
        ctx = _build_context(items)

        assert "Blockers (must complete first): id1, id2" in ctx

    def test_empty_items_list(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _build_context

        ctx = _build_context([])
        assert "planning assistant" in ctx


class TestExtractEdges:
    def test_valid_edges_returned(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _extract_edges

        tool_input = {"edges": [{"from_item_id": "a", "to_item_id": "b"}]}
        edges = _extract_edges(tool_input, {"a", "b"})

        assert len(edges) == 1
        assert edges[0].from_item_id == "a"
        assert edges[0].to_item_id == "b"

    def test_unknown_to_id_filtered(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _extract_edges

        tool_input = {"edges": [{"from_item_id": "a", "to_item_id": "unknown"}]}
        edges = _extract_edges(tool_input, {"a", "b"})

        assert edges == []

    def test_unknown_from_id_filtered(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _extract_edges

        tool_input = {"edges": [{"from_item_id": "unknown", "to_item_id": "b"}]}
        edges = _extract_edges(tool_input, {"a", "b"})

        assert edges == []

    def test_empty_edges(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _extract_edges

        edges = _extract_edges({"edges": []}, {"a", "b"})
        assert edges == []

    def test_malformed_edges_skipped(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _extract_edges

        tool_input = {
            "edges": ["not-a-dict", None, {"from_item_id": "a", "to_item_id": "b"}]
        }
        edges = _extract_edges(tool_input, {"a", "b"})

        assert len(edges) == 1

    def test_missing_edges_key(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _extract_edges

        edges = _extract_edges({}, {"a", "b"})
        assert edges == []
