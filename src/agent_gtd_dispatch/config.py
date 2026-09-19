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

# Retention — the decay-rate split.
# Evidence (transcript, completion artifact, patch) is small and stays useful for
# months; the workspace TREE is hundreds of MB to GB (full clones plus target/,
# node_modules/, .venv) and its value decays in a day or two.
EVIDENCE_ROOT: Path = Path.home() / "run-evidence"
EVIDENCE_RETENTION_DAYS: int = 30
WORKSPACE_RETENTION_HOURS: int = 48
RETENTION_INTERVAL_SECONDS: int = 3600

# Agent limits
MAX_TURNS: int = 100
TIMEOUT_SECONDS: int = 30 * 60  # 30 minutes
MANAGE_TIMEOUT_SECONDS: int = 4 * 60 * 60  # 4 hours for multi-wave manage runs
MAX_MANAGE_RETRIES: int = 2  # max auto-recovery relaunches for manage mode
MAX_CONCURRENT_RUNS: int = 32  # thread-pool ceiling for run_in_executor
CANCEL_GRACE_SECONDS: int = 5  # seconds between SIGTERM and SIGKILL on cancel

# Floor on remaining run budget below which the push backstop (worker completes an
# agent's unfinished `git push` after a successful build-mode exit) is not attempted
# at all — not enough time left to plausibly succeed (e.g. a slow pre-push hook).
PUSH_BACKSTOP_MIN_SECONDS: int = 10

# Minimum seconds granted to the post-run gate even when the build used most of
# its run timeout — a build that finishes with only seconds left on the clock
# still gets a real shot at running the project's quality gate.
POST_RUN_GATE_MIN_SECONDS: int = 600

# Per-subprocess timeout for the pre-launch gate install (hook-manager install,
# hook-dir lookup, or `.agent-gtd/setup`). See gates.py.
GATE_INSTALL_TIMEOUT_SECONDS: int = 300

# Watchdog (manage-agent staleness detection)
# Set above the longest build a manager may wait on: a manager has no polling
# heartbeat, so its state timestamp only advances on real progress. Too low and
# the watchdog kills a healthy manager mid-wait, burning MAX_MANAGE_RETRIES.
# Must stay < MANAGE_TIMEOUT_SECONDS.
MANAGE_STALE_THRESHOLD_SECONDS: int = 2100  # 35 min
WATCHDOG_INTERVAL_SECONDS: int = 180  # scan every 3 min

# Minimum agent-subprocess uptime (seconds) a manage run must have reached before
# its exit is eligible for a "free" (uncounted) relaunch. Below this, the manager
# is treated as crash-looping and the relaunch counts toward MAX_MANAGE_RETRIES —
# this is the crash-loop protection that keeps the free-relaunch exemption from
# masking a manager that dies immediately on every launch.
MANAGE_FREE_RELAUNCH_MIN_UPTIME_SECONDS: int = 120

# Lifetime cap (per rollout) on free (uncounted) manage relaunches granted while a
# child build run is healthy and still in flight. Bounds the free-relaunch
# exemption so a single stuck item cannot relaunch the manager forever without
# ever counting toward MAX_MANAGE_RETRIES.
MAX_MANAGE_FREE_RELAUNCHES: int = 25

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
# unauthenticated setups). Consumed by the talos-glm engine (native /api/chat wire)
# AND the claude-code-glm engine (Claude Code driving the Anthropic-compatible
# /v1/messages endpoint ollama.com also exposes). There is intentionally NO fallback
# to OLLAMA_API_KEY: mixing the two would silently ship the operator's local-server
# key to the cloud.
OLLAMA_CLOUD_API_KEY: str = ""

# Ollama Cloud model for the claude-code-glm engine. Claude Code reaches it via the
# Anthropic-compatible /v1/messages route ollama.com serves. claude-code-glm is the
# HARNESS twin of talos-glm (same cloud model + key, the Claude Code loop instead of
# talos), so this default must match talos-glm's pinned literal in talos.py. The env
# override steers claude-code-glm ONLY — talos-glm never reads it.
OLLAMA_CLOUD_BASE_URL: str = "https://ollama.com"
OLLAMA_CLOUD_MODEL: str = "glm-5.3:cloud"

# talos binary discovery: default 'talos', PATH-resolved by the subprocess machinery
# (mirrors how the 'claude' binary is resolved for claude-code engines). Override via
# TALOS_BIN env var when the binary lives at a non-default path on the host.
TALOS_BIN: str = "talos"
# Talos gate-command timeout. Talos' own default is 300 s; 900 s is chosen to
# survive a COLD fmt+clippy+nextest gate run on the Pi (pironman01).
TALOS_GATE_TIMEOUT_SECS: int = 900

# Root log level for THIS package's loggers.  `uvicorn.run()` configures only
# uvicorn's own loggers and leaves the root logger alone, so before 2026-09-19
# every `logger.info(...)` in this package went nowhere: the journal carried
# uvicorn access lines and nothing else.  That made the gate-install decisions,
# the post-run gate, the manage-recovery ladder and the unasserted-run WARNING
# invisible in production — all of them precisely the things you need when a
# dispatch misbehaves.  See `main.configure_logging`.
LOG_LEVEL: str = "INFO"


def load() -> None:
    """Load configuration from environment. Call once at startup."""
    global DISPATCH_API_KEY, AGENT_GTD_URL, AGENT_GTD_API_KEY
    global WORKSPACE_ROOT, MAX_TURNS, TIMEOUT_SECONDS, MANAGE_TIMEOUT_SECONDS
    global ANTHROPIC_API_KEY, PLANNER_MODEL, MAX_CONCURRENT_RUNS
    global OLLAMA_BASE_URL, OLLAMA_API_KEY, OLLAMA_DEFAULT_MODEL
    global OLLAMA_TIMEOUT_MULTIPLIER, CANCEL_GRACE_SECONDS, PUSH_BACKSTOP_MIN_SECONDS
    global GATE_INSTALL_TIMEOUT_SECONDS, POST_RUN_GATE_MIN_SECONDS
    global OLLAMA_CLOUD_API_KEY, OLLAMA_CLOUD_BASE_URL, OLLAMA_CLOUD_MODEL
    global TALOS_BIN, TALOS_GATE_TIMEOUT_SECS
    global AGENT_SUBPROCESS_USER
    global MANAGE_STALE_THRESHOLD_SECONDS, WATCHDOG_INTERVAL_SECONDS
    global PLANNER_PROVIDER, PLANNER_BEDROCK_MODEL, AWS_REGION
    global MANAGE_FREE_RELAUNCH_MIN_UPTIME_SECONDS, MAX_MANAGE_FREE_RELAUNCHES
    global EVIDENCE_ROOT, EVIDENCE_RETENTION_DAYS, WORKSPACE_RETENTION_HOURS
    global RETENTION_INTERVAL_SECONDS
    global LOG_LEVEL

    LOG_LEVEL = os.environ.get("DISPATCH_LOG_LEVEL", "INFO").strip().upper() or "INFO"

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
    _evidence_env = os.environ.get("DISPATCH_EVIDENCE_ROOT", "")
    if _evidence_env:
        EVIDENCE_ROOT = Path(_evidence_env)
    else:
        EVIDENCE_ROOT = WORKSPACE_ROOT.parent / "run-evidence"
    EVIDENCE_RETENTION_DAYS = int(
        os.environ.get("DISPATCH_EVIDENCE_RETENTION_DAYS", "30")
    )
    WORKSPACE_RETENTION_HOURS = int(
        os.environ.get("DISPATCH_WORKSPACE_RETENTION_HOURS", "48")
    )
    RETENTION_INTERVAL_SECONDS = int(
        os.environ.get("DISPATCH_RETENTION_INTERVAL_SECONDS", "3600")
    )
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
    OLLAMA_CLOUD_BASE_URL = os.environ.get(
        "OLLAMA_CLOUD_BASE_URL", "https://ollama.com"
    )
    OLLAMA_CLOUD_MODEL = os.environ.get("OLLAMA_CLOUD_MODEL", "glm-5.3:cloud")
    TALOS_BIN = os.environ.get("TALOS_BIN", "talos")
    TALOS_GATE_TIMEOUT_SECS = int(os.environ.get("TALOS_GATE_TIMEOUT_SECS", "900"))
    OLLAMA_DEFAULT_MODEL = os.environ.get("OLLAMA_DEFAULT_MODEL", "qwen3.6:35b")
    OLLAMA_TIMEOUT_MULTIPLIER = float(
        os.environ.get("OLLAMA_TIMEOUT_MULTIPLIER", "2.0")
    )
    CANCEL_GRACE_SECONDS = int(os.environ.get("DISPATCH_CANCEL_GRACE_SECONDS", "5"))
    PUSH_BACKSTOP_MIN_SECONDS = int(
        os.environ.get("DISPATCH_PUSH_BACKSTOP_MIN_SECONDS", "10")
    )
    GATE_INSTALL_TIMEOUT_SECONDS = int(
        os.environ.get("DISPATCH_GATE_INSTALL_TIMEOUT_SECONDS", "300")
    )
    POST_RUN_GATE_MIN_SECONDS = int(
        os.environ.get("DISPATCH_POST_RUN_GATE_MIN_SECONDS", "600")
    )
    MANAGE_STALE_THRESHOLD_SECONDS = int(
        os.environ.get("DISPATCH_MANAGE_STALE_THRESHOLD_SECONDS", "2100")
    )
    WATCHDOG_INTERVAL_SECONDS = int(
        os.environ.get("DISPATCH_WATCHDOG_INTERVAL_SECONDS", "180")
    )
    MANAGE_FREE_RELAUNCH_MIN_UPTIME_SECONDS = int(
        os.environ.get("DISPATCH_MANAGE_FREE_RELAUNCH_MIN_UPTIME_SECONDS", "120")
    )
    MAX_MANAGE_FREE_RELAUNCHES = int(
        os.environ.get("DISPATCH_MAX_MANAGE_FREE_RELAUNCHES", "25")
    )
