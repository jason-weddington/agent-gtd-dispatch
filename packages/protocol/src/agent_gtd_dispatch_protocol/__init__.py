"""Wire-contract schemas for the agent-gtd-dispatch API.

This package is the single source of truth for the dispatch service's
HTTP wire contract. Both the dispatch service and its callers (e.g.
agent-gtd) should depend on this package rather than duplicating models.
"""

from __future__ import annotations

from agent_gtd_dispatch_protocol.branches import make_branch_name as make_branch_name
from agent_gtd_dispatch_protocol.models import DagEdge as DagEdge
from agent_gtd_dispatch_protocol.models import DispatchRequest as DispatchRequest
from agent_gtd_dispatch_protocol.models import PlanRequest as PlanRequest
from agent_gtd_dispatch_protocol.models import RolloutPlan as RolloutPlan
from agent_gtd_dispatch_protocol.models import RunResponse as RunResponse
from agent_gtd_dispatch_protocol.models import RunStatus as RunStatus

__all__ = [
    "DagEdge",
    "DispatchRequest",
    "PlanRequest",
    "RolloutPlan",
    "RunResponse",
    "RunStatus",
    "make_branch_name",
]
