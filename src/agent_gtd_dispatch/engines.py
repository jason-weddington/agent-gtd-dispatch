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


def build_env(engine: Engine) -> dict[str, str]:
    """Build a filtered env dict for the engine's subprocess."""
    allowed = COMMON_ENV_KEYS | engine.env_keys
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env["HOME"] = str(Path.home())
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
    # Kiro has no --system-prompt or --max-turns; bake into the user prompt
    full_prompt = f"{system_prompt}\n\n---\n\n## Task\n\n{title}"
    cmd = ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools"]
    if agent_name:
        cmd.extend(["--agent", agent_name])
    cmd.append(full_prompt)
    return cmd


# --- Engine instances ---

CLAUDE = Engine(
    name="claude",
    binary="claude",
    auth_env_key="ANTHROPIC_API_KEY",
    env_keys=frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}),
    build_command=_build_claude_command,
)

KIRO = Engine(
    name="kiro",
    binary="kiro-cli",
    auth_env_key="KIRO_API_KEY",
    env_keys=frozenset({"KIRO_API_KEY"}),
    build_command=_build_kiro_command,
)

ENGINES: dict[str, Engine] = {
    "claude": CLAUDE,
    "kiro": KIRO,
}


def get_engine(name: str) -> Engine:
    """Look up an engine by name, raising ValueError if unknown."""
    try:
        return ENGINES[name]
    except KeyError:
        msg = f"Unknown engine: {name!r}. Available: {sorted(ENGINES)}"
        raise ValueError(msg) from None
