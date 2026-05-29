"""Agent engine definitions for headless CLI backends."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_gtd_dispatch_protocol.models import DispatchMode

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


def build_env(
    engine: Engine, mode: DispatchMode = DispatchMode.BUILD
) -> dict[str, str]:
    """Build a filtered env dict for the engine's subprocess."""
    import pwd

    from . import config  # local import so module is readable before config.load()

    allowed = COMMON_ENV_KEYS | engine.env_keys
    # Manage-mode env exposure: add dispatch URL + key for claude manage-mode executors
    if engine.name == "claude-code" and mode == DispatchMode.MANAGE:
        allowed = allowed | frozenset(_MANAGE_EXECUTOR_ENV_KEYS)
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env["HOME"] = str(Path.home())
    if engine.extra_env_fn is not None:
        env.update(engine.extra_env_fn())

    # Prepend ~/.local/bin for the agent user so uvx/MCP binaries (personal-kb,
    # agent-gtd) are discoverable after sudo's env_reset strips PATH.
    if config.AGENT_SUBPROCESS_USER:
        try:
            pw = pwd.getpwnam(config.AGENT_SUBPROCESS_USER)
            local_bin = Path(pw.pw_dir) / ".local" / "bin"
        except KeyError:
            local_bin = Path.home() / ".local" / "bin"
    else:
        local_bin = Path.home() / ".local" / "bin"

    local_bin_str = str(local_bin)
    current_path = env.get("PATH", "")
    if local_bin_str not in current_path.split(":"):
        env["PATH"] = (
            f"{local_bin_str}:{current_path}" if current_path else local_bin_str
        )

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
        "--model",
        "opus",
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


def _build_claude_ollama_command(
    system_prompt: str,
    title: str,
    max_turns: int,
    agent_name: str | None,
) -> list[str]:
    """Build command for claude-code-ollama: same as CLAUDE but injects --model."""
    from . import config  # local import so module is readable before config.load()

    cmd = [
        "claude",
        "--model",
        config.OLLAMA_DEFAULT_MODEL,
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


def _claude_ollama_extra_env() -> dict[str, str]:
    """Extra env vars injected into the claude-code-ollama subprocess."""
    from . import config  # local import so module is readable before config.load()

    return {
        "ANTHROPIC_BASE_URL": config.OLLAMA_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": config.OLLAMA_API_KEY,
    }


def _build_claude_sonnet_command(
    system_prompt: str,
    title: str,
    max_turns: int,
    agent_name: str | None,
) -> list[str]:
    """Build command for claude-code-sonnet: same as CLAUDE but uses moving alias."""
    cmd = [
        "claude",
        "--model",
        "sonnet",
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


def _build_claude_haiku_command(
    system_prompt: str,
    title: str,
    max_turns: int,
    agent_name: str | None,
) -> list[str]:
    """Build command for claude-code-haiku: same as CLAUDE but uses moving alias."""
    cmd = [
        "claude",
        "--model",
        "haiku",
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


# --- Engine instances ---

CLAUDE = Engine(
    name="claude-code",
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
    build_command=_build_claude_ollama_command,
    extra_env_fn=_claude_ollama_extra_env,
)

CLAUDE_SONNET = Engine(
    name="claude-code-sonnet",
    binary="claude",
    auth_env_key="CLAUDE_CODE_OAUTH_TOKEN",
    # ANTHROPIC_API_KEY is deliberately NOT exposed — see kb-01512.
    env_keys=frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}),
    build_command=_build_claude_sonnet_command,
)

CLAUDE_HAIKU = Engine(
    name="claude-code-haiku",
    binary="claude",
    auth_env_key="CLAUDE_CODE_OAUTH_TOKEN",
    # ANTHROPIC_API_KEY is deliberately NOT exposed — see kb-01512.
    env_keys=frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}),
    build_command=_build_claude_haiku_command,
)

ENGINES: dict[str, Engine] = {
    "claude-code": CLAUDE,
    "kiro": KIRO,
    "claude-code-ollama": CLAUDE_OLLAMA,
    "claude-code-sonnet": CLAUDE_SONNET,
    "claude-code-haiku": CLAUDE_HAIKU,
}

# Engine names that share the Claude Code auth path (OAuth token OR API key)
_CLAUDE_CODE_ENGINES: frozenset[str] = frozenset(
    {"claude-code", "claude-code-sonnet", "claude-code-haiku"}
)


def is_engine_available(engine: Engine) -> bool:
    """Return True if the host env has credentials for this engine.

    Claude Code engines accept either CLAUDE_CODE_OAUTH_TOKEN (Max
    subscription) or ANTHROPIC_API_KEY (pay-as-you-go) — either presence
    is sufficient. Kiro requires KIRO_API_KEY. The Ollama-routed Claude
    engine requires OLLAMA_BASE_URL to be configured.
    """
    name = engine.name
    if name in _CLAUDE_CODE_ENGINES:
        return bool(
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY")
        )
    if name == "kiro":
        return bool(os.environ.get("KIRO_API_KEY"))
    if name == "claude-code-ollama":
        from . import config

        return bool(config.OLLAMA_BASE_URL)
    return False


def get_available_engine_names() -> list[str]:
    """Return registered engine names whose credentials are present in the env."""
    return [name for name, engine in ENGINES.items() if is_engine_available(engine)]


def get_engine(name: str) -> Engine:
    """Look up an engine by name, raising ValueError if unknown."""
    try:
        return ENGINES[name]
    except KeyError:
        msg = f"Unknown engine: {name!r}. Available: {sorted(ENGINES)}"
        raise ValueError(msg) from None
