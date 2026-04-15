"""Data models for dispatch runs."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    """Status of a dispatch run."""

    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"
    cancelled = "cancelled"


class Run(BaseModel):
    """A single dispatch run record."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    item_id: str
    project_name: str
    branch_name: str
    status: RunStatus = RunStatus.pending
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DispatchRequest(BaseModel):
    """Request body for the /dispatch endpoint."""

    item_id: str
    max_turns: int


class RunResponse(BaseModel):
    """Response model for run endpoints."""

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
