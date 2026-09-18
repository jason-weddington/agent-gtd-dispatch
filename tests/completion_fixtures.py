"""Shared helpers for seeding the build-completion evidence a BUILD run requires.

Every BUILD-mode terminal now reads two things out of the workspace: the CLI's
``--output-format json`` result envelope at the tail of ``transcript.txt`` (leg 1)
and the agent-authored artifact at ``.dispatch/completion.json`` (leg 2).  Worker
tests that only care about push verification or the post-run gate seed a clean,
green pair with these helpers so the assertion under test stays isolated.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

SUCCESS_ENVELOPE: dict[str, Any] = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 12,
    "session_id": "s-1",
    "total_cost_usd": 0.4,
}

MAX_TURNS_ENVELOPE: dict[str, Any] = {
    "type": "result",
    "subtype": "error_max_turns",
    "is_error": True,
    "stop_reason": "tool_use",
    "terminal_reason": "max_turns",
}


def write_envelope(workspace: Path, **overrides: Any) -> None:
    """Write a result envelope as the last line of the workspace transcript."""
    envelope = dict(SUCCESS_ENVELOPE)
    envelope.update(overrides)
    (workspace / "transcript.txt").write_text(json.dumps(envelope) + "\n")


def write_artifact(workspace: Path, disposition: str = "done", **fields: Any) -> None:
    """Write a completion artifact at the contract path under ``workspace``."""
    payload: dict[str, Any] = {
        "schema_version": 1,
        "disposition": disposition,
        "summary": "seeded by test",
    }
    payload.update(fields)
    target = workspace / ".dispatch"
    target.mkdir(parents=True, exist_ok=True)
    (target / "completion.json").write_text(json.dumps(payload))


def seed_build_evidence(workspace: Path, disposition: str = "done") -> None:
    """Seed a green envelope plus a ``done`` artifact into ``workspace``."""
    workspace.mkdir(parents=True, exist_ok=True)
    write_envelope(workspace)
    write_artifact(workspace, disposition)
