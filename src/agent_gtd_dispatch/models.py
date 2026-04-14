"""Data models for dispatch runs."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"
    cancelled = "cancelled"


class Run(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    item_id: str
    project_name: str
    branch_name: str
    status: RunStatus = RunStatus.pending
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    error: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class DispatchRequest(BaseModel):
    item_id: str
    max_turns: int | None = None


class RunResponse(BaseModel):
    id: str
    item_id: str
    project_name: str
    branch_name: str
    status: RunStatus
    started_at: datetime | None
    completed_at: datetime | None
    exit_code: int | None
    error: str | None
    created_at: datetime
