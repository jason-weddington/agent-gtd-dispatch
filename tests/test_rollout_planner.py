"""Tests for rollout_planner module."""

from __future__ import annotations

import logging
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

    def test_unexpected_top_level_key_logs_warning(self, caplog) -> None:
        from agent_gtd_dispatch.rollout_planner import _extract_edges

        tool_input = {
            "edges": [{"from_item_id": "a", "to_item_id": "b"}],
            "extra_field": "unexpected",
        }
        with caplog.at_level(
            logging.WARNING, logger="agent_gtd_dispatch.rollout_planner"
        ):
            edges = _extract_edges(tool_input, {"a", "b"})

        assert len(edges) == 1  # edge still extracted despite extra key
        assert any("extra_field" in msg for msg in caplog.messages)

    def test_unexpected_edge_key_logs_warning(self, caplog) -> None:
        from agent_gtd_dispatch.rollout_planner import _extract_edges

        tool_input = {
            "edges": [{"from_item_id": "a", "to_item_id": "b", "reason": "semantic"}]
        }
        with caplog.at_level(
            logging.WARNING, logger="agent_gtd_dispatch.rollout_planner"
        ):
            edges = _extract_edges(tool_input, {"a", "b"})

        assert len(edges) == 1  # edge still extracted despite extra key
        assert any("reason" in msg for msg in caplog.messages)

    def test_missing_from_item_id_logs_warning(self, caplog) -> None:
        from agent_gtd_dispatch.rollout_planner import _extract_edges

        tool_input = {"edges": [{"to_item_id": "b"}]}  # from_item_id absent
        with caplog.at_level(
            logging.WARNING, logger="agent_gtd_dispatch.rollout_planner"
        ):
            edges = _extract_edges(tool_input, {"a", "b"})

        assert edges == []  # edge dropped because from_item_id not in valid_ids
        assert any("from_item_id" in msg for msg in caplog.messages)

    def test_missing_to_item_id_logs_warning(self, caplog) -> None:
        from agent_gtd_dispatch.rollout_planner import _extract_edges

        tool_input = {"edges": [{"from_item_id": "a"}]}  # to_item_id absent
        with caplog.at_level(
            logging.WARNING, logger="agent_gtd_dispatch.rollout_planner"
        ):
            edges = _extract_edges(tool_input, {"a", "b"})

        assert edges == []  # edge dropped because to_item_id not in valid_ids
        assert any("to_item_id" in msg for msg in caplog.messages)

    def test_misspelled_from_item_key_logs_warning(self, caplog) -> None:
        """Regression: 'from_item' (should be 'from_item_id') triggers warnings."""
        from agent_gtd_dispatch.rollout_planner import _extract_edges

        tool_input = {"edges": [{"from_item": "a", "to_item_id": "b"}]}
        with caplog.at_level(
            logging.WARNING, logger="agent_gtd_dispatch.rollout_planner"
        ):
            edges = _extract_edges(tool_input, {"a", "b"})

        assert edges == []  # edge dropped — from_item_id key absent
        all_messages = " ".join(caplog.messages)
        # warns about the unexpected key present
        assert "from_item" in all_messages
        # warns about the expected key that is missing
        assert "from_item_id" in all_messages

    def test_no_warnings_on_clean_input(self, caplog) -> None:
        from agent_gtd_dispatch.rollout_planner import _extract_edges

        tool_input = {"edges": [{"from_item_id": "a", "to_item_id": "b"}]}
        with caplog.at_level(
            logging.WARNING, logger="agent_gtd_dispatch.rollout_planner"
        ):
            edges = _extract_edges(tool_input, {"a", "b"})

        assert len(edges) == 1
        assert caplog.records == []


class TestPathsOverlap:
    def test_identical_paths_overlap(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _paths_overlap

        assert _paths_overlap("src/foo.py", "src/foo.py") is True

    def test_different_paths_do_not_overlap(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _paths_overlap

        assert _paths_overlap("src/foo.py", "src/bar.py") is False

    def test_trailing_slash_directory_overlaps_nested_file(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _paths_overlap

        assert (
            _paths_overlap(
                "agent-gtd-dispatch/tests/", "agent-gtd-dispatch/tests/test_foo.py"
            )
            is True
        )
        assert (
            _paths_overlap(
                "agent-gtd-dispatch/tests/test_foo.py", "agent-gtd-dispatch/tests/"
            )
            is True
        )

    def test_no_extension_directory_overlaps_nested_file(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _paths_overlap

        assert (
            _paths_overlap(
                "agent-gtd-dispatch/tests", "agent-gtd-dispatch/tests/test_foo.py"
            )
            is True
        )
        assert (
            _paths_overlap(
                "agent-gtd-dispatch/tests/test_foo.py", "agent-gtd-dispatch/tests"
            )
            is True
        )

    def test_directory_does_not_overlap_sibling_file(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _paths_overlap

        # src/foo is a dir; src/foobar.py is NOT beneath it
        assert _paths_overlap("src/foo", "src/foobar.py") is False

    def test_file_with_extension_not_treated_as_directory(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _paths_overlap

        # setup-dispatch-host.sh has an extension → not a directory
        assert (
            _paths_overlap(
                "agent-gtd-dispatch/setup-dispatch-host.sh",
                "agent-gtd-dispatch/setup-dispatch-host.sh/extra",
            )
            is False
        )

    def test_normalized_trailing_slash_matches(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _paths_overlap

        # "src/foo/" vs "src/foo" — same after normalisation
        assert _paths_overlap("src/foo/", "src/foo/") is True
        assert _paths_overlap("src/foo/", "src/foo") is True


class TestComputeOverlapEdges:
    def test_two_items_same_exact_path(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _compute_overlap_edges

        items = [
            {
                "id": "a",
                "files_to_modify": [
                    {"path": "agent-gtd-dispatch/setup.sh", "change": "add line"}
                ],
            },
            {
                "id": "b",
                "files_to_modify": [
                    {"path": "agent-gtd-dispatch/setup.sh", "change": "remove line"}
                ],
            },
        ]
        edges = _compute_overlap_edges(items, ["a", "b"])

        assert len(edges) == 1
        assert edges[0].from_item_id == "a"
        assert edges[0].to_item_id == "b"

    def test_no_shared_paths_no_edges(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _compute_overlap_edges

        items = [
            {"id": "a", "files_to_modify": [{"path": "src/foo.py", "change": "x"}]},
            {"id": "b", "files_to_modify": [{"path": "src/bar.py", "change": "y"}]},
        ]
        edges = _compute_overlap_edges(items, ["a", "b"])

        assert edges == []

    def test_three_items_same_file_yields_chain(self) -> None:
        """Regression: three items sharing the same file must be fully serialised."""
        from agent_gtd_dispatch.rollout_planner import _compute_overlap_edges

        items = [
            {
                "id": "a",
                "files_to_modify": [{"path": "agent_gtd/README.md", "change": "A"}],
            },
            {
                "id": "b",
                "files_to_modify": [{"path": "agent_gtd/README.md", "change": "B"}],
            },
            {
                "id": "c",
                "files_to_modify": [{"path": "agent_gtd/README.md", "change": "C"}],
            },
        ]
        edges = _compute_overlap_edges(items, ["a", "b", "c"])

        edge_pairs = {(e.from_item_id, e.to_item_id) for e in edges}
        # All three pairs must be present so the items are totally ordered
        assert ("a", "b") in edge_pairs
        assert ("a", "c") in edge_pairs
        assert ("b", "c") in edge_pairs

    def test_four_items_same_file_fully_serialised(self) -> None:
        """Regression for AL2023-feedback wave: four items editing same shell script."""
        from agent_gtd_dispatch.rollout_planner import _compute_overlap_edges

        path = "agent-gtd-dispatch/setup-dispatch-host.sh"
        items = [
            {"id": str(i), "files_to_modify": [{"path": path, "change": ""}]}
            for i in range(4)
        ]
        edges = _compute_overlap_edges(items, [str(i) for i in range(4)])

        edge_pairs = {(e.from_item_id, e.to_item_id) for e in edges}
        # Every earlier→later pair must have an edge
        for i in range(4):
            for j in range(i + 1, 4):
                assert (str(i), str(j)) in edge_pairs, f"Missing edge {i}→{j}"

    def test_directory_overlap_with_nested_file(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _compute_overlap_edges

        items = [
            {
                "id": "a",
                "files_to_modify": [
                    {"path": "agent-gtd-dispatch/tests/", "change": "dir"}
                ],
            },
            {
                "id": "b",
                "files_to_modify": [
                    {"path": "agent-gtd-dispatch/tests/test_foo.py", "change": "file"}
                ],
            },
        ]
        edges = _compute_overlap_edges(items, ["a", "b"])

        assert len(edges) == 1
        assert edges[0].from_item_id == "a"
        assert edges[0].to_item_id == "b"

    def test_preserves_input_order_for_edge_direction(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _compute_overlap_edges

        items = [
            {"id": "b", "files_to_modify": [{"path": "shared.py", "change": ""}]},
            {"id": "a", "files_to_modify": [{"path": "shared.py", "change": ""}]},
        ]
        # b comes before a in item_ids, so edge should be b→a
        edges = _compute_overlap_edges(items, ["b", "a"])

        assert len(edges) == 1
        assert edges[0].from_item_id == "b"
        assert edges[0].to_item_id == "a"

    def test_empty_files_to_modify_no_edges(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _compute_overlap_edges

        items = [
            {"id": "a", "files_to_modify": []},
            {"id": "b", "files_to_modify": []},
        ]
        edges = _compute_overlap_edges(items, ["a", "b"])

        assert edges == []


class TestMergeEdges:
    def test_disjoint_edges_combined(self) -> None:
        from agent_gtd_dispatch.models import DagEdge
        from agent_gtd_dispatch.rollout_planner import _merge_edges

        llm = [DagEdge(from_item_id="a", to_item_id="b")]
        overlap = [DagEdge(from_item_id="b", to_item_id="c")]
        result = _merge_edges(llm, overlap)

        assert len(result) == 2

    def test_duplicate_edges_deduplicated(self) -> None:
        from agent_gtd_dispatch.models import DagEdge
        from agent_gtd_dispatch.rollout_planner import _merge_edges

        llm = [DagEdge(from_item_id="a", to_item_id="b")]
        overlap = [DagEdge(from_item_id="a", to_item_id="b")]
        result = _merge_edges(llm, overlap)

        assert len(result) == 1

    def test_llm_edge_preserved_first(self) -> None:
        from agent_gtd_dispatch.models import DagEdge
        from agent_gtd_dispatch.rollout_planner import _merge_edges

        llm = [DagEdge(from_item_id="a", to_item_id="b")]
        result = _merge_edges(llm, [])

        assert result[0].from_item_id == "a"

    def test_empty_inputs(self) -> None:
        from agent_gtd_dispatch.rollout_planner import _merge_edges

        assert _merge_edges([], []) == []


class TestPlanRolloutOverlapRegression:
    """Regression tests: same-file overlap edges appear regardless of LLM output."""

    async def test_llm_misses_shared_file_overlap_edge_added_deterministically(
        self,
    ) -> None:
        """LLM returns no edges; deterministic post-processing must add the edge."""
        items = [
            {
                "id": "id1",
                "title": "Item 1",
                "description": "",
                "blockers": [],
                "files_to_modify": [
                    {"path": "agent-gtd-dispatch/setup-dispatch-host.sh", "change": "A"}
                ],
                "acceptance_criteria": [],
            },
            {
                "id": "id2",
                "title": "Item 2",
                "description": "",
                "blockers": [],
                "files_to_modify": [
                    {"path": "agent-gtd-dispatch/setup-dispatch-host.sh", "change": "B"}
                ],
                "acceptance_criteria": [],
            },
        ]
        # LLM deliberately returns no edges (simulates the bug)
        mock_response = _make_tool_response([])
        mock_gtd = _make_gtd_client(items)
        mock_anthropic = _make_anthropic_module(mock_response)

        with (
            patch("agent_gtd_dispatch.rollout_planner.gtd_client", mock_gtd),
            patch("agent_gtd_dispatch.rollout_planner.anthropic", mock_anthropic),
        ):
            from agent_gtd_dispatch.rollout_planner import plan_rollout

            result = await plan_rollout(["id1", "id2"])

        # Must have the overlap edge despite LLM returning nothing
        assert len(result.edges) == 1
        assert result.edges[0].from_item_id == "id1"
        assert result.edges[0].to_item_id == "id2"

    async def test_four_items_same_file_all_serialised(self) -> None:
        """Regression for AL2023-feedback wave: four items → no parallel execution."""
        shared_path = "agent-gtd-dispatch/setup-dispatch-host.sh"
        ids = ["i1", "i2", "i3", "i4"]
        items = [
            {
                "id": iid,
                "title": f"Item {iid}",
                "description": "",
                "blockers": [],
                "files_to_modify": [{"path": shared_path, "change": ""}],
                "acceptance_criteria": [],
            }
            for iid in ids
        ]
        # LLM returns only a partial chain — missing cross-pairs
        mock_response = _make_tool_response(
            [
                {"from_item_id": "i1", "to_item_id": "i2"},
                {"from_item_id": "i3", "to_item_id": "i4"},
            ]
        )
        mock_gtd = _make_gtd_client(items)
        mock_anthropic = _make_anthropic_module(mock_response)

        with (
            patch("agent_gtd_dispatch.rollout_planner.gtd_client", mock_gtd),
            patch("agent_gtd_dispatch.rollout_planner.anthropic", mock_anthropic),
        ):
            from agent_gtd_dispatch.rollout_planner import plan_rollout

            result = await plan_rollout(ids)

        edge_pairs = {(e.from_item_id, e.to_item_id) for e in result.edges}
        # Every earlier→later pair must have an edge
        for i, id_a in enumerate(ids):
            for id_b in ids[i + 1 :]:
                assert (id_a, id_b) in edge_pairs, f"Missing edge {id_a}→{id_b}"

    async def test_llm_semantic_edge_preserved_when_no_file_overlap(self) -> None:
        """LLM semantic edges are kept even when no file-path overlap exists."""
        items = [
            {
                "id": "id1",
                "title": "Item 1",
                "description": "",
                "blockers": [],
                "files_to_modify": [{"path": "src/foo.py", "change": ""}],
                "acceptance_criteria": [],
            },
            {
                "id": "id2",
                "title": "Item 2",
                "description": "",
                "blockers": [],
                "files_to_modify": [{"path": "src/bar.py", "change": ""}],
                "acceptance_criteria": [],
            },
        ]
        # LLM detects a semantic dependency (no file overlap)
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

        assert len(result.edges) == 1
        assert result.edges[0].from_item_id == "id1"
        assert result.edges[0].to_item_id == "id2"

    async def test_llm_edge_and_overlap_edge_deduplicated(self) -> None:
        """When LLM produces the overlap edge too, result contains it exactly once."""
        items = [
            {
                "id": "id1",
                "title": "Item 1",
                "description": "",
                "blockers": [],
                "files_to_modify": [{"path": "shared.py", "change": ""}],
                "acceptance_criteria": [],
            },
            {
                "id": "id2",
                "title": "Item 2",
                "description": "",
                "blockers": [],
                "files_to_modify": [{"path": "shared.py", "change": ""}],
                "acceptance_criteria": [],
            },
        ]
        # LLM also returns the overlap edge
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

        # Should appear exactly once
        assert len(result.edges) == 1


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
