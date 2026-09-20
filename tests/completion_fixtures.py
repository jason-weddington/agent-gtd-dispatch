"""Shared helpers for seeding the build-completion evidence a BUILD run requires.

A BUILD-mode terminal reads ONE thing out of the workspace: the CLI's
``--output-format json`` result envelope at the tail of ``transcript.txt``.  Worker
tests that only care about push verification or the post-run gate seed a clean, green
envelope with these helpers so the assertion under test stays isolated.

There used to be a second helper here for the agent-authored completion artifact.
It is gone with the contract — no code reads an agent-written file any more, so a test
that seeded one would be seeding something the worker cannot see.
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


def seed_build_evidence(workspace: Path) -> None:
    """Seed a green result envelope into ``workspace``."""
    workspace.mkdir(parents=True, exist_ok=True)
    write_envelope(workspace)
