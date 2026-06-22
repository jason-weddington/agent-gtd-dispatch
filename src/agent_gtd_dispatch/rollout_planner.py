"""Rollout planner: calls Claude to produce a dependency DAG for a set of GTD items."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from typing import Any, cast

import anthropic

from . import config, gtd_client
from .models import DagEdge, RolloutPlan

logger = logging.getLogger(__name__)

PRODUCE_DAG_TOOL = cast(
    "anthropic.types.ToolParam",
    {
        "name": "produce_dag",
        "description": (
            "Produce a dependency DAG for the given work items. "
            "An edge {from_item_id: A, to_item_id: B} means B must wait for A to "
            "complete first. Only reference item_ids from the provided list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "edges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "from_item_id": {
                                "type": "string",
                                "description": (
                                    "ID of the prerequisite item (must complete first)"
                                ),
                            },
                            "to_item_id": {
                                "type": "string",
                                "description": "ID of the dependent item",
                            },
                        },
                        "required": ["from_item_id", "to_item_id"],
                    },
                    "description": "Dependency edges between items.",
                },
            },
            "required": ["edges"],
        },
    },
)


def _is_directory_entry(path: str) -> bool:
    """Return True if path looks like a directory entry.

    Treats paths with a trailing slash, or whose last path component has no
    file-extension dot, as directory entries (e.g. ``src/foo/`` or ``src/foo``).
    """
    if path.endswith("/"):
        return True
    last_component = path.rstrip("/").rsplit("/", 1)[-1]
    return "." not in last_component


def _paths_overlap(path_a: str, path_b: str) -> bool:
    """Return True if *path_a* and *path_b* represent overlapping filesystem paths.

    Two paths overlap when they are identical (after normalising trailing
    slashes), or when one is a directory entry that is an ancestor of the
    other (e.g. ``agent-gtd-dispatch/tests/`` overlaps with
    ``agent-gtd-dispatch/tests/test_foo.py``).
    """
    norm_a = path_a.rstrip("/")
    norm_b = path_b.rstrip("/")
    if norm_a == norm_b:
        return True
    return (_is_directory_entry(path_a) and norm_b.startswith(norm_a + "/")) or (
        _is_directory_entry(path_b) and norm_a.startswith(norm_b + "/")
    )


def _compute_overlap_edges(
    items: list[dict[str, Any]], item_ids: list[str]
) -> list[DagEdge]:
    """Compute deterministic dependency edges for items with overlapping file paths.

    For any pair of items *(earlier, later)* — in input order — whose
    ``files_to_modify`` paths overlap, adds an edge ``earlier → later`` so
    that they are always serialised, regardless of what the LLM planner decided.

    Args:
        items: Full GTD item dicts (each must contain ``'id'`` and
            ``'files_to_modify'``).
        item_ids: Ordered list of item IDs that defines input ordering.

    Returns:
        List of DagEdge representing deterministic overlap constraints.
    """
    # Build a map from item_id → list of file paths
    id_to_paths: dict[str, list[str]] = {}
    for item in items:
        iid = str(item.get("id", ""))
        files = item.get("files_to_modify", [])
        paths: list[str] = []
        if isinstance(files, list):
            paths = [
                str(f.get("path", ""))
                for f in files
                if isinstance(f, dict) and f.get("path")
            ]
        id_to_paths[iid] = paths

    edges: list[DagEdge] = []
    for i, id_a in enumerate(item_ids):
        for id_b in item_ids[i + 1 :]:
            paths_a = id_to_paths.get(id_a, [])
            paths_b = id_to_paths.get(id_b, [])
            if any(_paths_overlap(pa, pb) for pa in paths_a for pb in paths_b):
                edges.append(DagEdge(from_item_id=id_a, to_item_id=id_b))
    return edges


def _merge_edges(
    llm_edges: list[DagEdge], overlap_edges: list[DagEdge]
) -> list[DagEdge]:
    """Union LLM-produced edges with deterministic overlap edges, deduplicating.

    LLM edges appear first in the result; overlap edges fill any gaps the LLM
    missed.  Duplicate ``(from_id, to_id)`` pairs are silently dropped.

    Args:
        llm_edges: Edges produced by the LLM planner.
        overlap_edges: Deterministic edges from file-path overlap analysis.

    Returns:
        Merged, deduplicated list of DagEdge.
    """
    seen: set[tuple[str, str]] = set()
    result: list[DagEdge] = []
    for edge in llm_edges + overlap_edges:
        key = (edge.from_item_id, edge.to_item_id)
        if key not in seen:
            seen.add(key)
            result.append(edge)
    return result


def _active_planner_model() -> str:
    """Return the model id that the planner will use for the current provider.

    Returns config.PLANNER_MODEL for the 'anthropic' provider and
    config.PLANNER_BEDROCK_MODEL for the 'bedrock' provider.
    """
    if config.PLANNER_PROVIDER == "bedrock":
        return config.PLANNER_BEDROCK_MODEL
    return config.PLANNER_MODEL


async def plan_rollout(item_ids: list[str]) -> RolloutPlan:
    """Fetch items concurrently and call Claude to produce a dependency DAG.

    After obtaining the LLM's semantic edges, the function deterministically
    augments them with overlap edges derived from shared ``files_to_modify``
    paths.  This guarantees that items touching the same file (or a directory
    that contains another item's file) are always serialised — regardless of
    whether the LLM detected the relationship.

    Args:
        item_ids: List of GTD item IDs to plan.

    Returns:
        RolloutPlan with nodes, edges, and the model used.  Edges are the
        union of LLM semantic edges and deterministic overlap edges.

    Raises:
        Exception: If gtd_client.get_item fails for any item, the exception
            propagates to the caller.
    """
    items_list = list(
        await asyncio.gather(*[gtd_client.get_item(iid) for iid in item_ids])
    )
    context = _build_context(items_list)
    active_model = _active_planner_model()
    client: anthropic.AsyncAnthropicBedrock | anthropic.AsyncAnthropic
    if config.PLANNER_PROVIDER == "bedrock":
        # Empty string must become None so the SDK falls back to the AWS_REGION
        # env var / us-east-1 default instead of a bogus empty region.
        client = anthropic.AsyncAnthropicBedrock(aws_region=config.AWS_REGION or None)
    else:
        client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=active_model,
        max_tokens=1024,
        tools=[PRODUCE_DAG_TOOL],
        tool_choice={"type": "tool", "name": "produce_dag"},
        messages=[{"role": "user", "content": context}],
    )
    tool_block = next(b for b in response.content if b.type == "tool_use")
    tool_input = cast("dict[str, Any]", tool_block.input)
    llm_edges = _extract_edges(tool_input, set(item_ids))
    overlap_edges = _compute_overlap_edges(items_list, item_ids)
    edges = _merge_edges(llm_edges, overlap_edges)
    _assert_acyclic(list(item_ids), edges)
    return RolloutPlan(nodes=list(item_ids), edges=edges, planner_model=active_model)


def _assert_acyclic(nodes: list[str], edges: list[DagEdge]) -> None:
    """Assert the graph defined by nodes and edges is acyclic (a valid DAG).

    Uses Kahn's topological-sort algorithm. If any cycle exists (including
    self-loops), raises ValueError rather than allowing a broken plan to be
    persisted and silently blocking items forever.

    Args:
        nodes: All node IDs in the graph.
        edges: Directed edges; an edge with from_item_id=A and to_item_id=B
            means A must complete before B.

    Raises:
        ValueError: If a cyclic dependency is detected.
    """
    in_degree: dict[str, int] = dict.fromkeys(nodes, 0)
    adjacency: defaultdict[str, list[str]] = defaultdict(list)

    for edge in edges:
        adjacency[edge.from_item_id].append(edge.to_item_id)
        in_degree[edge.to_item_id] = in_degree.get(edge.to_item_id, 0) + 1

    queue: deque[str] = deque(node for node in nodes if in_degree[node] == 0)
    processed = 0

    while queue:
        node = queue.popleft()
        processed += 1
        for successor in adjacency[node]:
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                queue.append(successor)

    if processed < len(nodes):
        raise ValueError(
            f"cyclic dependency detected in rollout plan: "
            f"{len(nodes) - processed} node(s) involved in a cycle"
        )


def _build_context(items: list[dict[str, Any]]) -> str:
    """Build the user message for the planner LLM call.

    Args:
        items: List of GTD item dicts, each with title, description, blockers,
            files_to_modify (list of {path, change} dicts), and acceptance_criteria
            (list of strings).

    Returns:
        Formatted context string for the planner prompt.
    """
    lines: list[str] = [
        "You are a planning assistant. Given a list of work items, identify dependency "
        "edges between them.\n"
        "An edge {from_item_id: A, to_item_id: B} means B must wait for A to "
        "complete.\n"
        "Derive edges from declared blockers and shared file paths in the structured "
        "`files_to_modify` field. Same file path across two items = candidate edge.\n"
        "Only reference item_ids from the provided list.\n",
    ]
    for item in items:
        item_id = str(item.get("id", ""))
        title = str(item.get("title", ""))
        description = str(item.get("description", ""))
        blockers = item.get("blockers", [])
        if isinstance(blockers, list) and blockers:
            blocker_ids = ", ".join(str(b) for b in blockers)
        else:
            blocker_ids = "none"
        files_to_modify = item.get("files_to_modify", [])
        if isinstance(files_to_modify, list) and files_to_modify:
            file_paths = ", ".join(
                str(f.get("path", "")) for f in files_to_modify if isinstance(f, dict)
            )
        else:
            file_paths = "none"
        acceptance_criteria = item.get("acceptance_criteria", [])
        lines.append(f"## Item {item_id}: {title}")
        lines.append(f"Blockers (must complete first): {blocker_ids}")
        lines.append(f"Files to modify: {file_paths}")
        lines.append("Acceptance criteria:")
        if isinstance(acceptance_criteria, list) and acceptance_criteria:
            for ac in acceptance_criteria:
                lines.append(str(ac))
        else:
            lines.append("none")
        lines.append("Description:")
        lines.append(description)
        lines.append("---")
    return "\n".join(lines)


def _extract_edges(tool_input: dict[str, Any], valid_ids: set[str]) -> list[DagEdge]:
    """Extract and validate edges from the LLM tool call response.

    Filters out any edge whose from_item_id or to_item_id is not in valid_ids,
    preventing the LLM from introducing unknown item references.

    Args:
        tool_input: The raw dict from the LLM tool call (tool_use block input).
        valid_ids: Set of valid item IDs from the original request.

    Returns:
        List of DagEdge where both endpoints are in valid_ids.
    """
    raw_edges = tool_input.get("edges", [])
    if not isinstance(raw_edges, list):
        return []
    edges: list[DagEdge] = []
    for raw in raw_edges:
        if not isinstance(raw, dict):
            continue
        from_id = raw.get("from_item_id", "")
        to_id = raw.get("to_item_id", "")
        if from_id in valid_ids and to_id in valid_ids:
            edges.append(DagEdge(from_item_id=str(from_id), to_item_id=str(to_id)))
    return edges
