"""Wire-contract models for the agent-gtd-dispatch API."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel


class RunStatus(StrEnum):
    """Status of a dispatch run."""

    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"
    cancelled = "cancelled"
    already_satisfied = "already_satisfied"


class DispatchMode(StrEnum):
    """Dispatch execution mode.

    ``REVIEW`` is the short-lived per-build merge reviewer the dispatch worker
    launches from its own wave loop.  It is a DISTINCT mode rather than a flag
    on a manage run on purpose: the manage relaunch ladder fires on
    ``run.mode == MANAGE and run.rollout_id`` and exists to resurrect a RESIDENT
    manager, so a reviewer exiting — which is normal completion, not a crash —
    must not look like one.  A separate mode also keeps the reviewer out of the
    manage-only env grant (``_MANAGE_EXECUTOR_ENV_KEYS``: a reviewer never
    dispatches) and gives it its own turn budget, without touching either the
    ladder or the manage path.
    """

    BUILD = "build"
    PLAN = "plan"
    MANAGE = "manage"
    REVIEW = "review"


class BuildEngine(StrEnum):
    """Canonical build-engine identifiers shared by the board and the dispatch service.

    The talos-* members are build-mode only.
    """

    CLAUDE_CODE = "claude-code"
    CLAUDE_CODE_OLLAMA = "claude-code-ollama"
    CLAUDE_CODE_GLM = "claude-code-glm"
    CLAUDE_CODE_SONNET = "claude-code-sonnet"
    CLAUDE_CODE_HAIKU = "claude-code-haiku"
    KIRO = "kiro"
    TALOS_HAIKU = "talos-haiku"
    TALOS_SONNET = "talos-sonnet"
    TALOS_OPUS = "talos-opus"
    TALOS_QWEN = "talos-qwen"
    TALOS_GLM = "talos-glm"
    TALOS_GLM_FLASH = "talos-glm-flash"


class DispatchRequest(BaseModel):
    """Request body for the /dispatch endpoint."""

    item_id: str | None = None
    max_turns: int
    engine: str = "claude-code"
    agent_name: str | None = None
    mode: DispatchMode = DispatchMode.BUILD
    timeout_minutes: int | None = None
    rollout_id: str | None = None
    attribution: str | None = None
    callback_token: str | None = None


class RunResponse(BaseModel):
    """Response model for run endpoints."""

    id: str
    item_id: str | None
    project_name: str
    branch_name: str | None
    engine: str
    engine_actual: str | None = None
    agent_name: str | None
    mode: DispatchMode
    rollout_id: str | None
    status: RunStatus
    started_at: datetime | None
    completed_at: datetime | None
    exit_code: int | None
    error: str | None
    completion: str | None = None
    created_at: datetime


__all__ = [
    "UTC",
    "BuildEngine",
    "DispatchMode",
    "DispatchRequest",
    "RunResponse",
    "RunStatus",
]
