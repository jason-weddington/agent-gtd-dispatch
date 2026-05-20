"""Data models for dispatch runs."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from agent_gtd_dispatch_protocol.models import DagEdge as DagEdge
from agent_gtd_dispatch_protocol.models import DispatchRequest as DispatchRequest
from agent_gtd_dispatch_protocol.models import PlanRequest as PlanRequest
from agent_gtd_dispatch_protocol.models import RolloutPlan as RolloutPlan
from agent_gtd_dispatch_protocol.models import RunResponse as _BaseRunResponse
from agent_gtd_dispatch_protocol.models import RunStatus as RunStatus
from pydantic import BaseModel, Field


class EngineSwap(BaseModel):
    """Describes an engine substitution made by the dispatch service."""

    from_engine: str
    to_engine: str
    reason: str


class InfoResponse(BaseModel):
    """Service identity, capacity, and capabilities returned by GET /info.

    Consumed by multi-host routers on the agent_gtd side to filter
    dispatch targets by available engines/agents and pick by capacity.
    """

    engine: str
    version: str
    max_concurrent_runs: int
    active_runs: int
    engines: list[str]
    agents: list[str]


class Run(BaseModel):
    """A single dispatch run record."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    item_id: str | None = None
    project_name: str
    branch_name: str | None = None
    engine: str = "claude-code"
    engine_actual: str | None = None
    agent_name: str | None = None
    mode: str = "build"
    rollout_id: str | None = None
    workspace_path: str | None = None
    status: RunStatus = RunStatus.pending
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunResponse(_BaseRunResponse):
    """Extended run response with engine swap visibility fields."""

    engine_actual: str | None = None
    engine_swap: EngineSwap | None = None
