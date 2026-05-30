"""Configuration from environment variables."""

from __future__ import annotations

import os
import urllib.parse
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
AGENT_SUBPROCESS_USER: str = ""

# Agent limits
MAX_TURNS: int = 100
TIMEOUT_SECONDS: int = 30 * 60  # 30 minutes
MANAGE_TIMEOUT_SECONDS: int = 4 * 60 * 60  # 4 hours for multi-wave manage runs
MAX_MANAGE_RETRIES: int = 2  # max auto-recovery relaunches for manage mode
MAX_CONCURRENT_RUNS: int = 32  # thread-pool ceiling for run_in_executor
CANCEL_GRACE_SECONDS: int = 5  # seconds between SIGTERM and SIGKILL on cancel

# Watchdog (manage-agent staleness detection)
MANAGE_STALE_THRESHOLD_SECONDS: int = 900  # 15 min; must stay < MANAGE_TIMEOUT_SECONDS
WATCHDOG_INTERVAL_SECONDS: int = 180  # scan every 3 min

# Planner (wave DAG)
ANTHROPIC_API_KEY: str = ""
PLANNER_MODEL: str = "claude-sonnet-4-6"

# Ollama local inference backend.
# OLLAMA_BASE_URL is the Ollama root URL, e.g. "http://10.0.0.5:11434".
# Do NOT include /v1 or any path suffix — Ollama exposes the Anthropic
# Messages API at the root. Empty = engine disabled.
OLLAMA_BASE_URL: str = ""
OLLAMA_API_KEY: str = "ollama"  # dummy value; Ollama ignores auth
OLLAMA_DEFAULT_MODEL: str = "qwen3.6:35b"
OLLAMA_TIMEOUT_MULTIPLIER: float = 2.0


def load() -> None:
    """Load configuration from environment. Call once at startup."""
    global DISPATCH_API_KEY, AGENT_GTD_URL, AGENT_GTD_API_KEY
    global WORKSPACE_ROOT, MAX_TURNS, TIMEOUT_SECONDS, MANAGE_TIMEOUT_SECONDS
    global ANTHROPIC_API_KEY, PLANNER_MODEL, MAX_CONCURRENT_RUNS
    global OLLAMA_BASE_URL, OLLAMA_API_KEY, OLLAMA_DEFAULT_MODEL
    global OLLAMA_TIMEOUT_MULTIPLIER, CANCEL_GRACE_SECONDS
    global AGENT_SUBPROCESS_USER
    global MANAGE_STALE_THRESHOLD_SECONDS, WATCHDOG_INTERVAL_SECONDS

    DISPATCH_API_KEY = _require("DISPATCH_API_KEY")
    AGENT_GTD_URL = _require("AGENT_GTD_URL")
    AGENT_GTD_API_KEY = _require("AGENT_GTD_API_KEY")
    ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")

    AGENT_SUBPROCESS_USER = os.environ.get("DISPATCH_AGENT_SUBPROCESS_USER", "")
    _workspace_env = os.environ.get("DISPATCH_WORKSPACE_ROOT", "")
    if _workspace_env:
        WORKSPACE_ROOT = Path(_workspace_env)
    elif AGENT_SUBPROCESS_USER:
        WORKSPACE_ROOT = Path.home().parent / AGENT_SUBPROCESS_USER / "workspace"
    else:
        WORKSPACE_ROOT = Path.home() / "workspace"
    MAX_TURNS = int(os.environ.get("DISPATCH_MAX_TURNS", "100"))
    TIMEOUT_SECONDS = int(os.environ.get("DISPATCH_TIMEOUT_SECONDS", "1800"))
    MANAGE_TIMEOUT_SECONDS = int(
        os.environ.get("DISPATCH_MANAGE_TIMEOUT_SECONDS", "14400")
    )
    PLANNER_MODEL = os.environ.get("DISPATCH_PLANNER_MODEL", "claude-sonnet-4-6")
    MAX_CONCURRENT_RUNS = int(os.environ.get("DISPATCH_MAX_CONCURRENT_RUNS", "32"))
    OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "")
    if OLLAMA_BASE_URL:
        _parsed = urllib.parse.urlparse(OLLAMA_BASE_URL)
        if _parsed.scheme not in ("http", "https") or not _parsed.netloc:
            msg = (
                f"Invalid OLLAMA_BASE_URL={OLLAMA_BASE_URL!r}: must start with "
                f"http:// or https:// and include a hostname. "
                f"Expected format: http://host:port — got {OLLAMA_BASE_URL!r}"
            )
            raise ValueError(msg)
    OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "ollama")
    OLLAMA_DEFAULT_MODEL = os.environ.get("OLLAMA_DEFAULT_MODEL", "qwen3.6:35b")
    OLLAMA_TIMEOUT_MULTIPLIER = float(
        os.environ.get("OLLAMA_TIMEOUT_MULTIPLIER", "2.0")
    )
    CANCEL_GRACE_SECONDS = int(os.environ.get("DISPATCH_CANCEL_GRACE_SECONDS", "5"))
    MANAGE_STALE_THRESHOLD_SECONDS = int(
        os.environ.get("DISPATCH_MANAGE_STALE_THRESHOLD_SECONDS", "900")
    )
    WATCHDOG_INTERVAL_SECONDS = int(
        os.environ.get("DISPATCH_WATCHDOG_INTERVAL_SECONDS", "180")
    )
