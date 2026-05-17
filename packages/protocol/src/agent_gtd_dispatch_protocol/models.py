"""Wire-contract models for the agent-gtd-dispatch API."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    """Status of a dispatch run."""

    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"
    cancelled = "cancelled"


class DispatchRequest(BaseModel):
    """Request body for the /dispatch endpoint."""

    item_id: str | None = None
    max_turns: int
    engine: str = "claude-code"
    agent_name: str | None = None
    mode: str = "build"
    timeout_minutes: int | None = None
    rollout_id: str | None = None
    attribution: str | None = None


class RunResponse(BaseModel):
    """Response model for run endpoints."""

    id: str
    item_id: str | None
    project_name: str
    branch_name: str | None
    engine: str
    agent_name: str | None
    mode: str
    rollout_id: str | None
    status: RunStatus
    started_at: datetime | None
    completed_at: datetime | None
    exit_code: int | None
    error: str | None
    created_at: datetime


class PlanRequest(BaseModel):
    """Request body for the /plan endpoint."""

    item_ids: list[str] = Field(min_length=1)


class DagEdge(BaseModel):
    """A directed dependency edge: from_item_id must complete before to_item_id."""

    from_item_id: str
    to_item_id: str


class RolloutPlan(BaseModel):
    """Result of the planner: a dependency DAG for a set of items."""

    nodes: list[str]
    edges: list[DagEdge]
    planner_model: str


__all__ = [
    "UTC",
    "DagEdge",
    "DispatchRequest",
    "PlanRequest",
    "RolloutPlan",
    "RunResponse",
    "RunStatus",
]
