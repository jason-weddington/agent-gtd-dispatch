"""Configuration from environment variables."""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from typing import Literal


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
# Set above the longest build a manager may wait on: a manager has no polling
# heartbeat, so its state timestamp only advances on real progress. Too low and
# the watchdog kills a healthy manager mid-wait, burning MAX_MANAGE_RETRIES.
# Must stay < MANAGE_TIMEOUT_SECONDS.
MANAGE_STALE_THRESHOLD_SECONDS: int = 2100  # 35 min
WATCHDOG_INTERVAL_SECONDS: int = 180  # scan every 3 min

# Planner (wave DAG)
ANTHROPIC_API_KEY: str = ""
PLANNER_MODEL: str = "claude-sonnet-4-6"
PLANNER_PROVIDER: Literal["anthropic", "bedrock"] = "anthropic"
PLANNER_BEDROCK_MODEL: str = "global.anthropic.claude-sonnet-4-6"
AWS_REGION: str = ""

# Ollama local inference backend.
# OLLAMA_BASE_URL is the Ollama root URL, e.g. "http://10.0.0.5:11434".
# Do NOT include /v1 or any path suffix — Ollama exposes the Anthropic
# Messages API at the root. Empty = engine disabled.
OLLAMA_BASE_URL: str = ""
OLLAMA_API_KEY: str = "ollama"  # dummy value; Ollama ignores auth
OLLAMA_DEFAULT_MODEL: str = "qwen3.6:35b"
OLLAMA_TIMEOUT_MULTIPLIER: float = 2.0

# Ollama Cloud API key (https://ollama.com) — distinct from the LOCAL OLLAMA_API_KEY
# above, which points at the operator's own Ollama server (dummy 'ollama' on
# unauthenticated setups). Consumed ONLY by the talos-glm engine, which routes to
# ollama.com. There is intentionally NO fallback to OLLAMA_API_KEY: mixing the two
# would silently ship the operator's local-server key to the cloud.
OLLAMA_CLOUD_API_KEY: str = ""

# talos binary discovery: default 'talos', PATH-resolved by the subprocess machinery
# (mirrors how the 'claude' binary is resolved for claude-code engines). Override via
# TALOS_BIN env var when the binary lives at a non-default path on the host.
TALOS_BIN: str = "talos"
# Talos gate-command timeout. Talos' own default is 300 s; 900 s is chosen to
# survive a COLD fmt+clippy+nextest gate run on the Pi (pironman01).
TALOS_GATE_TIMEOUT_SECS: int = 900


def load() -> None:
    """Load configuration from environment. Call once at startup."""
    global DISPATCH_API_KEY, AGENT_GTD_URL, AGENT_GTD_API_KEY
    global WORKSPACE_ROOT, MAX_TURNS, TIMEOUT_SECONDS, MANAGE_TIMEOUT_SECONDS
    global ANTHROPIC_API_KEY, PLANNER_MODEL, MAX_CONCURRENT_RUNS
    global OLLAMA_BASE_URL, OLLAMA_API_KEY, OLLAMA_DEFAULT_MODEL
    global OLLAMA_TIMEOUT_MULTIPLIER, CANCEL_GRACE_SECONDS
    global OLLAMA_CLOUD_API_KEY, TALOS_BIN, TALOS_GATE_TIMEOUT_SECS
    global AGENT_SUBPROCESS_USER
    global MANAGE_STALE_THRESHOLD_SECONDS, WATCHDOG_INTERVAL_SECONDS
    global PLANNER_PROVIDER, PLANNER_BEDROCK_MODEL, AWS_REGION

    DISPATCH_API_KEY = _require("DISPATCH_API_KEY")
    AGENT_GTD_URL = _require("AGENT_GTD_URL")
    AGENT_GTD_API_KEY = _require("AGENT_GTD_API_KEY")

    _provider_raw = os.environ.get("DISPATCH_PLANNER_PROVIDER", "anthropic")
    if _provider_raw not in {"anthropic", "bedrock"}:
        msg = (
            f"DISPATCH_PLANNER_PROVIDER={_provider_raw!r}: "
            f"must be 'anthropic' or 'bedrock'"
        )
        raise RuntimeError(msg)
    PLANNER_PROVIDER = _provider_raw  # type: ignore[assignment]

    if PLANNER_PROVIDER == "anthropic":
        ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
    else:
        ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

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
    PLANNER_BEDROCK_MODEL = os.environ.get(
        "DISPATCH_PLANNER_BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6"
    )
    AWS_REGION = os.environ.get("AWS_REGION", "")
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
    # NO fallback to OLLAMA_API_KEY — cloud vs. local Ollama servers use distinct
    # credentials and mixing them ships the local key to the cloud.
    OLLAMA_CLOUD_API_KEY = os.environ.get("OLLAMA_CLOUD_API_KEY", "")
    TALOS_BIN = os.environ.get("TALOS_BIN", "talos")
    TALOS_GATE_TIMEOUT_SECS = int(os.environ.get("TALOS_GATE_TIMEOUT_SECS", "900"))
    OLLAMA_DEFAULT_MODEL = os.environ.get("OLLAMA_DEFAULT_MODEL", "qwen3.6:35b")
    OLLAMA_TIMEOUT_MULTIPLIER = float(
        os.environ.get("OLLAMA_TIMEOUT_MULTIPLIER", "2.0")
    )
    CANCEL_GRACE_SECONDS = int(os.environ.get("DISPATCH_CANCEL_GRACE_SECONDS", "5"))
    MANAGE_STALE_THRESHOLD_SECONDS = int(
        os.environ.get("DISPATCH_MANAGE_STALE_THRESHOLD_SECONDS", "2100")
    )
    WATCHDOG_INTERVAL_SECONDS = int(
        os.environ.get("DISPATCH_WATCHDOG_INTERVAL_SECONDS", "180")
    )
