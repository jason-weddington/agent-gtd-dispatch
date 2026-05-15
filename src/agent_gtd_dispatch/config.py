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
MANAGE_TIMEOUT_SECONDS: int = 4 * 60 * 60  # 4 hours for multi-wave manage runs
MAX_MANAGE_RETRIES: int = 2  # max auto-recovery relaunches for manage mode
MAX_CONCURRENT_RUNS: int = 32  # thread-pool ceiling for run_in_executor

# Planner (wave DAG)
ANTHROPIC_API_KEY: str = ""
PLANNER_MODEL: str = "claude-sonnet-4-6"

# Ollama local inference backend
OLLAMA_BASE_URL: str = ""  # e.g. "http://10.0.0.5:11434/v1"; empty = disabled
OLLAMA_API_KEY: str = "ollama"  # dummy value; Ollama ignores auth
OLLAMA_DEFAULT_MODEL: str = "qwen3.5:35b"
OLLAMA_TIMEOUT_MULTIPLIER: float = 2.0


def load() -> None:
    """Load configuration from environment. Call once at startup."""
    global DISPATCH_API_KEY, AGENT_GTD_URL, AGENT_GTD_API_KEY
    global WORKSPACE_ROOT, MAX_TURNS, TIMEOUT_SECONDS, MANAGE_TIMEOUT_SECONDS
    global ANTHROPIC_API_KEY, PLANNER_MODEL, MAX_CONCURRENT_RUNS
    global OLLAMA_BASE_URL, OLLAMA_API_KEY, OLLAMA_DEFAULT_MODEL
    global OLLAMA_TIMEOUT_MULTIPLIER

    DISPATCH_API_KEY = _require("DISPATCH_API_KEY")
    AGENT_GTD_URL = _require("AGENT_GTD_URL")
    AGENT_GTD_API_KEY = _require("AGENT_GTD_API_KEY")
    ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")

    WORKSPACE_ROOT = Path(
        os.environ.get("DISPATCH_WORKSPACE_ROOT", str(Path.home() / "workspace"))
    )
    MAX_TURNS = int(os.environ.get("DISPATCH_MAX_TURNS", "100"))
    TIMEOUT_SECONDS = int(os.environ.get("DISPATCH_TIMEOUT_SECONDS", "1800"))
    MANAGE_TIMEOUT_SECONDS = int(
        os.environ.get("DISPATCH_MANAGE_TIMEOUT_SECONDS", "14400")
    )
    PLANNER_MODEL = os.environ.get("DISPATCH_PLANNER_MODEL", "claude-sonnet-4-6")
    MAX_CONCURRENT_RUNS = int(os.environ.get("DISPATCH_MAX_CONCURRENT_RUNS", "32"))
    OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "")
    OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "ollama")
    OLLAMA_DEFAULT_MODEL = os.environ.get("OLLAMA_DEFAULT_MODEL", "qwen3.5:35b")
    OLLAMA_TIMEOUT_MULTIPLIER = float(
        os.environ.get("OLLAMA_TIMEOUT_MULTIPLIER", "2.0")
    )
