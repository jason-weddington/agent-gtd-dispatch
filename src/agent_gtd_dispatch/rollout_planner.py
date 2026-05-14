"""Rollout planner: calls Claude to produce a dependency DAG for a set of GTD items."""

from __future__ import annotations

import asyncio
import logging
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


async def plan_rollout(item_ids: list[str]) -> RolloutPlan:
    """Fetch items concurrently and call Claude to produce a dependency DAG.

    Args:
        item_ids: List of GTD item IDs to plan.

    Returns:
        RolloutPlan with nodes, edges, and the model used.

    Raises:
        Exception: If gtd_client.get_item fails for any item, the exception
            propagates to the caller.
    """
    items = await asyncio.gather(*[gtd_client.get_item(iid) for iid in item_ids])
    context = _build_context(list(items))
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=config.PLANNER_MODEL,
        max_tokens=1024,
        tools=[PRODUCE_DAG_TOOL],
        tool_choice={"type": "tool", "name": "produce_dag"},
        messages=[{"role": "user", "content": context}],
    )
    tool_block = next(b for b in response.content if b.type == "tool_use")
    tool_input = cast("dict[str, Any]", tool_block.input)
    edges = _extract_edges(tool_input, set(item_ids))
    return RolloutPlan(
        nodes=list(item_ids), edges=edges, planner_model=config.PLANNER_MODEL
    )


def _build_context(items: list[dict[str, Any]]) -> str:
    """Build the user message for the planner LLM call.

    Args:
        items: List of GTD item dicts, each with title, description, and blockers.

    Returns:
        Formatted context string for the planner prompt.
    """
    lines: list[str] = [
        "You are a planning assistant. Given a list of work items, identify dependency "
        "edges between them.\n"
        "An edge {from_item_id: A, to_item_id: B} means B must wait for A to "
        "complete.\n"
        "Derive edges from: declared blockers, 'Files to modify' overlaps in "
        "descriptions, and phrases like 'Depends on X' in AC text.\n"
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
        lines.append(f"## Item {item_id}: {title}")
        lines.append(f"Blockers (must complete first): {blocker_ids}")
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
