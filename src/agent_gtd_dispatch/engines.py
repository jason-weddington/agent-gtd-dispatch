"""Agent engine definitions for headless CLI backends."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# Env vars shared by all engines — safe to pass to any subprocess
COMMON_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "TERM",
        "SHELL",
        "AGENT_GTD_URL",
        "AGENT_GTD_API_KEY",
        "KB_DATABASE_URL",
        "SSH_AUTH_SOCK",
        "GIT_SSH_COMMAND",
    }
)


@dataclass(frozen=True, slots=True)
class Engine:
    """Configuration for a headless agent CLI backend."""

    name: str
    binary: str
    auth_env_key: str
    env_keys: frozenset[str]
    build_command: Callable[[str, str, int, str | None], list[str]]
    extra_env_fn: Callable[[], dict[str, str]] | None = None


# Manage-mode env exposure
# DISPATCH_LOCAL_URL and DISPATCH_API_KEY are passed to manage-mode claude executors
# so they can call back to the dispatch worker's /ci-gate endpoint.
_MANAGE_EXECUTOR_ENV_KEYS: tuple[str, ...] = ("DISPATCH_LOCAL_URL", "DISPATCH_API_KEY")


def build_env(engine: Engine, mode: str = "build") -> dict[str, str]:
    """Build a filtered env dict for the engine's subprocess."""
    allowed = COMMON_ENV_KEYS | engine.env_keys
    # Manage-mode env exposure: add dispatch URL + key for claude manage-mode executors
    if engine.name == "claude" and mode == "manage":
        allowed = allowed | frozenset(_MANAGE_EXECUTOR_ENV_KEYS)
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env["HOME"] = str(Path.home())
    if engine.extra_env_fn is not None:
        env.update(engine.extra_env_fn())
    return env


# --- Command builders ---


def _build_claude_command(
    system_prompt: str,
    title: str,
    max_turns: int,
    agent_name: str | None,
) -> list[str]:
    cmd = [
        "claude",
        "--dangerously-skip-permissions",
        "--max-turns",
        str(max_turns),
        "--system-prompt",
        system_prompt,
        "--print",
    ]
    if agent_name:
        cmd.extend(["--agent", agent_name])
    cmd.append(title)
    return cmd


def _build_kiro_command(
    system_prompt: str,
    title: str,
    max_turns: int,  # Kiro has no --max-turns flag
    agent_name: str | None,
) -> list[str]:
    # system_prompt.md is written to workspace by run_agent before this runs
    cmd = ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools"]
    if agent_name:
        cmd.extend(["--agent", agent_name])
    cmd.append(
        "Use the read tool to open system_prompt.md in this directory, "
        "then follow every instruction inside it."
    )
    return cmd


def _claude_ollama_extra_env() -> dict[str, str]:
    """Extra env vars injected into the claude-code-ollama subprocess."""
    from . import config  # local import so module is readable before config.load()

    return {
        "ANTHROPIC_BASE_URL": config.OLLAMA_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": config.OLLAMA_API_KEY,
        "ANTHROPIC_MODEL": config.OLLAMA_DEFAULT_MODEL,
    }


# --- Engine instances ---

CLAUDE = Engine(
    name="claude",
    binary="claude",
    auth_env_key="CLAUDE_CODE_OAUTH_TOKEN",
    # ANTHROPIC_API_KEY is deliberately NOT exposed to Claude Code subprocesses.
    # If it leaks through, Claude Code prefers API billing over the user's Max
    # subscription — see kb-01512.  The planner (rollout_planner.py) reads the key
    # via config.ANTHROPIC_API_KEY in-process, never via the subprocess env.
    env_keys=frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}),
    build_command=_build_claude_command,
)

KIRO = Engine(
    name="kiro",
    binary="kiro-cli",
    auth_env_key="KIRO_API_KEY",
    env_keys=frozenset({"KIRO_API_KEY"}),
    build_command=_build_kiro_command,
)

CLAUDE_OLLAMA = Engine(
    name="claude-code-ollama",
    binary="claude",
    auth_env_key="",  # auth is injected via extra_env_fn, not from parent env
    env_keys=frozenset(),  # no keys inherited from parent env; all via extra_env_fn
    build_command=_build_claude_command,
    extra_env_fn=_claude_ollama_extra_env,
)

ENGINES: dict[str, Engine] = {
    "claude": CLAUDE,
    "kiro": KIRO,
    "claude-code-ollama": CLAUDE_OLLAMA,
}


def get_engine(name: str) -> Engine:
    """Look up an engine by name, raising ValueError if unknown."""
    try:
        return ENGINES[name]
    except KeyError:
        msg = f"Unknown engine: {name!r}. Available: {sorted(ENGINES)}"
        raise ValueError(msg) from None
