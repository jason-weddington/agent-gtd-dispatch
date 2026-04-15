"""Configuration from environment variables."""

from __future__ import annotations

import os
from pathlib import Path


def _require(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        msg = f"Required environment variable {name} is not set"
        raise RuntimeError(msg)
    return val


# Dispatch API auth
DISPATCH_API_KEY: str = ""

# Agent GTD API
AGENT_GTD_URL: str = ""
AGENT_GTD_API_KEY: str = ""

# Workspace
WORKSPACE_ROOT: Path = Path.home() / "workspace"

# Agent limits
MAX_TURNS: int = 50
TIMEOUT_SECONDS: int = 30 * 60  # 30 minutes


def load() -> None:
    """Load configuration from environment. Call once at startup."""
    global DISPATCH_API_KEY, AGENT_GTD_URL, AGENT_GTD_API_KEY
    global WORKSPACE_ROOT, MAX_TURNS, TIMEOUT_SECONDS

    DISPATCH_API_KEY = _require("DISPATCH_API_KEY")
    AGENT_GTD_URL = _require("AGENT_GTD_URL")
    AGENT_GTD_API_KEY = _require("AGENT_GTD_API_KEY")

    WORKSPACE_ROOT = Path(
        os.environ.get("DISPATCH_WORKSPACE_ROOT", str(Path.home() / "workspace"))
    )
    MAX_TURNS = int(os.environ.get("DISPATCH_MAX_TURNS", "20"))
    TIMEOUT_SECONDS = int(os.environ.get("DISPATCH_TIMEOUT_SECONDS", "1800"))
