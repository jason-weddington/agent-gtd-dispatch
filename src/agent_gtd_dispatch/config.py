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
MAX_TURNS: int = 100
TIMEOUT_SECONDS: int = 30 * 60  # 30 minutes

# Wave manager
WAVE_MANAGER_ALLOWLIST_PATH: Path = Path("wave_manager/allowlist.yaml")

# Planner (wave DAG)
ANTHROPIC_API_KEY: str = ""
PLANNER_MODEL: str = "claude-sonnet-4-6"


def load() -> None:
    """Load configuration from environment. Call once at startup."""
    global DISPATCH_API_KEY, AGENT_GTD_URL, AGENT_GTD_API_KEY
    global WORKSPACE_ROOT, MAX_TURNS, TIMEOUT_SECONDS
    global WAVE_MANAGER_ALLOWLIST_PATH
    global ANTHROPIC_API_KEY, PLANNER_MODEL

    DISPATCH_API_KEY = _require("DISPATCH_API_KEY")
    AGENT_GTD_URL = _require("AGENT_GTD_URL")
    AGENT_GTD_API_KEY = _require("AGENT_GTD_API_KEY")
    ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")

    WORKSPACE_ROOT = Path(
        os.environ.get("DISPATCH_WORKSPACE_ROOT", str(Path.home() / "workspace"))
    )
    MAX_TURNS = int(os.environ.get("DISPATCH_MAX_TURNS", "100"))
    TIMEOUT_SECONDS = int(os.environ.get("DISPATCH_TIMEOUT_SECONDS", "1800"))
    WAVE_MANAGER_ALLOWLIST_PATH = Path(
        os.environ.get("WAVE_MANAGER_ALLOWLIST_PATH", "wave_manager/allowlist.yaml")
    )
    PLANNER_MODEL = os.environ.get("DISPATCH_PLANNER_MODEL", "claude-sonnet-4-6")
