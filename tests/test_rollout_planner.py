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

    async def test_shared_file_path_produces_edge(self) -> None:
        """Two items with the same file in files_to_modify; LLM sees path and returns edge."""
        items = [
            {
                "id": "id1",
                "title": "Item 1",
                "description": "",
                "blockers": [],
                "files_to_modify": [{"path": "src/shared.py", "change": "add helper"}],
                "acceptance_criteria": [],
            },
            {
                "id": "id2",
                "title": "Item 2",
                "description": "",
                "blockers": [],
                "files_to_modify": [{"path": "src/shared.py", "change": "use helper"}],
                "acceptance_criteria": [],
            },
        ]
        mock_response = _make_tool_response(
            [{"from_item_id": "id1", "to_item_id": "id2"}]
        )
        mock_gtd = _make_gtd_client(items)
        mock_api_client = MagicMock()
        mock_api_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic = MagicMock()
        mock_anthropic.AsyncAnthropic.return_value = mock_api_client

        with (
            patch("agent_gtd_dispatch.rollout_planner.gtd_client", mock_gtd),
            patch("agent_gtd_dispatch.rollout_planner.anthropic", mock_anthropic),
        ):
            from agent_gtd_dispatch.rollout_planner import plan_rollout

            result = await plan_rollout(["id1", "id2"])

        # Edge is returned
        assert len(result.edges) == 1
        assert result.edges[0].from_item_id == "id1"
        assert result.edges[0].to_item_id == "id2"

        # Shared path appeared in the context passed to the LLM
        call_args = mock_api_client.messages.create.call_args
        context_str = call_args.kwargs["messages"][0]["content"]
        assert "src/shared.py" in context_str

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

    async def test_cyclic_llm_output_raises(self) -> None:
        items = [
            {"id": "id1", "title": "Item 1", "description": "", "blockers": []},
            {"id": "id2", "title": "Item 2", "description": "", "blockers": []},
        ]
        # LLM hallucinates a two-node cycle: id1 -> id2 -> id1
        mock_response = _make_tool_response(
            [
                {"from_item_id": "id1", "to_item_id": "id2"},
                {"from_item_id": "id2", "to_item_id": "id1"},
            ]
        )
        mock_gtd = _make_gtd_client(items)
        mock_anthropic = _make_anthropic_module(mock_response)

        with (
            patch("agent_gtd_dispatch.rollout_planner.gtd_client", mock_gtd),
            patch("agent_gtd_dispatch.rollout_planner.anthropic", mock_anthropic),
        ):
            from agent_gtd_dispatch.rollout_planner import plan_rollout

            with pytest.raises(ValueError, match="cyclic"):
                await plan_rollout(["id1", "id2"])


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

    def test_files_to_modify_paths_rendered(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _build_context

        items = [
            {
                "id": "id1",
                "title": "T",
                "description": "",
                "blockers": [],
                "files_to_modify": [
                    {"path": "src/foo.py", "change": "add function"},
                    {"path": "src/bar.py", "change": "update import"},
                ],
            }
        ]
        ctx = _build_context(items)

        assert "src/foo.py" in ctx
        assert "src/bar.py" in ctx
        # change descriptions must NOT appear
        assert "add function" not in ctx
        assert "update import" not in ctx

    def test_acceptance_criteria_rendered(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _build_context

        items = [
            {
                "id": "id1",
                "title": "T",
                "description": "",
                "blockers": [],
                "acceptance_criteria": ["AC-1: Do X", "AC-2: Do Y"],
            }
        ]
        ctx = _build_context(items)

        assert "AC-1: Do X" in ctx
        assert "AC-2: Do Y" in ctx

    def test_empty_files_to_modify_shows_none(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _build_context

        items = [
            {
                "id": "id1",
                "title": "T",
                "description": "",
                "blockers": [],
                "files_to_modify": [],
            }
        ]
        ctx = _build_context(items)

        assert "Files to modify: none" in ctx


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


class TestProviderSelection:
    """Tests for DISPATCH_PLANNER_PROVIDER env-gating.

    All bedrock-path tests patch `agent_gtd_dispatch.rollout_planner.anthropic`
    so that AsyncAnthropicBedrock is a MagicMock — no live AWS calls are made.
    """

    async def test_provider_unset_uses_async_anthropic(self, tmp_path) -> None:
        items = [
            {"id": "id1", "title": "A", "description": "", "blockers": []},
        ]
        mock_response = _make_tool_response([])
        mock_gtd = _make_gtd_client(items)
        mock_anthropic = _make_anthropic_module(mock_response)

        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        }
        # DISPATCH_PLANNER_PROVIDER intentionally absent
        with patch.dict(os.environ, env, clear=False):
            from agent_gtd_dispatch import config as cfg

            cfg.load()
            with (
                patch("agent_gtd_dispatch.rollout_planner.gtd_client", mock_gtd),
                patch("agent_gtd_dispatch.rollout_planner.anthropic", mock_anthropic),
            ):
                from agent_gtd_dispatch.rollout_planner import plan_rollout

                await plan_rollout(["id1"])

        mock_anthropic.AsyncAnthropic.assert_called_once()
        mock_anthropic.AsyncAnthropicBedrock.assert_not_called()

    async def test_provider_anthropic_explicit_uses_async_anthropic(
        self, tmp_path
    ) -> None:
        items = [
            {"id": "id1", "title": "A", "description": "", "blockers": []},
        ]
        mock_response = _make_tool_response([])
        mock_gtd = _make_gtd_client(items)
        mock_anthropic = _make_anthropic_module(mock_response)

        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "DISPATCH_PLANNER_PROVIDER": "anthropic",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        }
        with patch.dict(os.environ, env, clear=False):
            from agent_gtd_dispatch import config as cfg

            cfg.load()
            with (
                patch("agent_gtd_dispatch.rollout_planner.gtd_client", mock_gtd),
                patch("agent_gtd_dispatch.rollout_planner.anthropic", mock_anthropic),
            ):
                from agent_gtd_dispatch.rollout_planner import plan_rollout

                await plan_rollout(["id1"])

        mock_anthropic.AsyncAnthropic.assert_called_once()
        mock_anthropic.AsyncAnthropicBedrock.assert_not_called()

    async def test_provider_bedrock_uses_async_anthropic_bedrock(
        self, tmp_path
    ) -> None:
        # No live AWS calls — anthropic module is fully mocked.
        items = [
            {"id": "id1", "title": "A", "description": "", "blockers": []},
            {"id": "id2", "title": "B", "description": "", "blockers": []},
        ]
        mock_response = _make_tool_response(
            [{"from_item_id": "id1", "to_item_id": "id2"}]
        )
        mock_gtd = _make_gtd_client(items)
        # Build bedrock-capable mock
        mock_api_client = MagicMock()
        mock_api_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic = MagicMock()
        mock_anthropic.AsyncAnthropicBedrock.return_value = mock_api_client

        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "k",
            "DISPATCH_PLANNER_PROVIDER": "bedrock",
            "DISPATCH_PLANNER_BEDROCK_MODEL": "global.anthropic.claude-sonnet-4-6",
            "AWS_REGION": "us-east-1",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        }
        # ANTHROPIC_API_KEY deliberately absent to prove bedrock path doesn't need it
        with patch.dict(os.environ, env, clear=False):
            # Remove ANTHROPIC_API_KEY from the patched env if it leaked from autouse
            import os as _os

            from agent_gtd_dispatch import config as cfg

            _os.environ.pop("ANTHROPIC_API_KEY", None)
            cfg.load()
            with (
                patch("agent_gtd_dispatch.rollout_planner.gtd_client", mock_gtd),
                patch("agent_gtd_dispatch.rollout_planner.anthropic", mock_anthropic),
            ):
                from agent_gtd_dispatch.rollout_planner import plan_rollout

                result = await plan_rollout(["id1", "id2"])

        # AsyncAnthropicBedrock called with aws_region kwarg
        mock_anthropic.AsyncAnthropicBedrock.assert_called_once_with(
            aws_region="us-east-1"
        )
        mock_anthropic.AsyncAnthropic.assert_not_called()

        # messages.create called with the bedrock model id
        call_kwargs = mock_api_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "global.anthropic.claude-sonnet-4-6"

        # planner_model reflects the bedrock model id
        assert result.planner_model == "global.anthropic.claude-sonnet-4-6"

    async def test_provider_bedrock_shared_response_handling(self, tmp_path) -> None:
        """Edge extraction and DAG acyclicity checks work on the bedrock path."""
        items = [
            {"id": "id1", "title": "A", "description": "", "blockers": []},
            {"id": "id2", "title": "B", "description": "", "blockers": []},
            {"id": "id3", "title": "C", "description": "", "blockers": []},
        ]
        # A linear chain: id1 -> id2 -> id3
        mock_response = _make_tool_response(
            [
                {"from_item_id": "id1", "to_item_id": "id2"},
                {"from_item_id": "id2", "to_item_id": "id3"},
            ]
        )
        mock_gtd = _make_gtd_client(items)
        mock_api_client = MagicMock()
        mock_api_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic = MagicMock()
        mock_anthropic.AsyncAnthropicBedrock.return_value = mock_api_client

        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "k",
            "DISPATCH_PLANNER_PROVIDER": "bedrock",
            "DISPATCH_PLANNER_BEDROCK_MODEL": "global.anthropic.claude-sonnet-4-6",
            "AWS_REGION": "eu-west-1",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        }
        with patch.dict(os.environ, env, clear=False):
            import os as _os

            _os.environ.pop("ANTHROPIC_API_KEY", None)
            from agent_gtd_dispatch import config as cfg

            cfg.load()
            with (
                patch("agent_gtd_dispatch.rollout_planner.gtd_client", mock_gtd),
                patch("agent_gtd_dispatch.rollout_planner.anthropic", mock_anthropic),
            ):
                from agent_gtd_dispatch.rollout_planner import plan_rollout

                result = await plan_rollout(["id1", "id2", "id3"])

        assert len(result.edges) == 2
        assert result.edges[0].from_item_id == "id1"
        assert result.edges[0].to_item_id == "id2"
        assert result.edges[1].from_item_id == "id2"
        assert result.edges[1].to_item_id == "id3"
        assert result.planner_model == "global.anthropic.claude-sonnet-4-6"


class TestProviderConfig:
    """Tests for config.load() with DISPATCH_PLANNER_PROVIDER env var."""

    def test_invalid_provider_raises_runtime_error(self, tmp_path) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "k",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "DISPATCH_PLANNER_PROVIDER": "claude",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        }
        with patch.dict(os.environ, env, clear=False):
            from agent_gtd_dispatch import config as cfg

            with pytest.raises(RuntimeError, match="must be 'anthropic' or 'bedrock'"):
                cfg.load()

    def test_bedrock_provider_load_succeeds_without_anthropic_api_key(
        self, tmp_path
    ) -> None:
        env = {
            "DISPATCH_API_KEY": "k",
            "AGENT_GTD_URL": "http://localhost:9999",
            "AGENT_GTD_API_KEY": "k",
            "DISPATCH_PLANNER_PROVIDER": "bedrock",
            "DISPATCH_WORKSPACE_ROOT": str(tmp_path),
        }
        with patch.dict(os.environ, env, clear=False):
            import os as _os

            _os.environ.pop("ANTHROPIC_API_KEY", None)
            from agent_gtd_dispatch import config as cfg

            # Should not raise even though ANTHROPIC_API_KEY is absent
            cfg.load()
            assert cfg.PLANNER_PROVIDER == "bedrock"


class TestAssertAcyclic:
    def _make_edges(self, pairs: list[tuple[str, str]]):
        from agent_gtd_dispatch.models import DagEdge

        return [DagEdge(from_item_id=f, to_item_id=t) for f, t in pairs]

    def test_self_loop_raises(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _assert_acyclic

        edges = self._make_edges([("A", "A")])
        with pytest.raises(ValueError, match="cyclic"):
            _assert_acyclic(["A"], edges)

    def test_two_node_cycle_raises(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _assert_acyclic

        edges = self._make_edges([("A", "B"), ("B", "A")])
        with pytest.raises(ValueError, match="cyclic"):
            _assert_acyclic(["A", "B"], edges)

    def test_three_node_cycle_raises(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _assert_acyclic

        edges = self._make_edges([("A", "B"), ("B", "C"), ("C", "A")])
        with pytest.raises(ValueError, match="cyclic"):
            _assert_acyclic(["A", "B", "C"], edges)

    def test_linear_chain_passes(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _assert_acyclic

        edges = self._make_edges([("A", "B"), ("B", "C")])
        _assert_acyclic(["A", "B", "C"], edges)  # should not raise

    def test_diamond_dag_passes(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _assert_acyclic

        edges = self._make_edges([("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")])
        _assert_acyclic(["A", "B", "C", "D"], edges)  # should not raise

    def test_empty_edges_passes(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _assert_acyclic

        _assert_acyclic(["A", "B", "C"], [])  # should not raise

    def test_single_node_no_edges_passes(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _assert_acyclic

        _assert_acyclic(["A"], [])  # should not raise
