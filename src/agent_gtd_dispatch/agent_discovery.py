"""Agent discovery — shells out to list_agents.sh and parses its output."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

logger = logging.getLogger(__name__)

# Engine identity — compile-time constant for this OSS repo.
# Forks wrapping a different engine should change this constant.
ENGINE_NAME: str = "claude-code"

try:
    SERVICE_VERSION: str = _pkg_version("agent-gtd-dispatch")
except PackageNotFoundError:
    SERVICE_VERSION = "unknown"

# Parsing constants
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_LINE_LEN = 4096  # 4 KiB safety cap


def _get_script_path() -> Path:
    """Return the hard-coded path to list_agents.sh.

    Kept as a function so tests can monkeypatch it to a fixture path.
    """
    return Path.home() / ".config" / "agent-dispatch" / "list_agents.sh"


def parse_list_agents_output(text: str) -> list[dict[str, str]]:
    r"""Parse the stdout of list_agents.sh into a list of agent dicts.

    Rules (from the list_agents.sh contract):

    - Blank lines are ignored.
    - Lines whose first non-whitespace character is ``#`` are comments (ignored).
    - Lines longer than 4 KiB are truncated (WARNING logged).
    - Each line has the shape ``<name>`` or ``<name>\t<description>``.
    - Name must match ``^[A-Za-z0-9_-]+$``; invalid names drop the line (WARNING).
    - Description: everything after the first tab, leading/trailing whitespace
      trimmed, internal tabs replaced with spaces.
    - Invalid UTF-8 is handled upstream before this function is called.

    Args:
        text: Decoded stdout from list_agents.sh.

    Returns:
        List of ``{"name": ..., "description": ...}`` dicts for valid lines.
    """
    agents: list[dict[str, str]] = []
    for line in text.splitlines():
        # Truncate lines exceeding the safety cap
        if len(line) > _MAX_LINE_LEN:
            logger.warning("Agent list line exceeds 4 KiB, truncating")
            line = line[:_MAX_LINE_LEN]

        # Skip blank lines
        if not line.strip():
            continue

        # Skip comment lines (first non-whitespace character is '#')
        if line.lstrip().startswith("#"):
            continue

        # Split on the first tab character
        parts = line.split("\t", 1)
        name = parts[0]
        description = ""
        if len(parts) > 1:
            # Normalise any further tabs in description to spaces, trim whitespace
            description = parts[1].replace("\t", " ").strip()

        # Validate name against the required pattern
        if not _NAME_RE.match(name):
            logger.warning("Invalid agent name %r, dropping line", name)
            continue

        agents.append({"name": name, "description": description})

    return agents


async def run_list_agents_script() -> list[dict[str, str]]:
    """Execute list_agents.sh and return the parsed agent list.

    Handles all failure modes gracefully — always returns a list (possibly
    empty) and never raises.

    Returns:
        List of ``{"name": ..., "description": ...}`` dicts, or ``[]`` on any
        error (missing script, permission denied, non-zero exit, timeout).
    """
    script_path = _get_script_path()

    if not script_path.exists():
        logger.debug("list_agents.sh not found at %s", script_path)
        return []

    if not os.access(script_path, os.X_OK):
        logger.warning("list_agents.sh is not executable: %s", script_path)
        return []

    proc = await asyncio.create_subprocess_exec(
        str(script_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=str(script_path.parent),
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=5.0
        )
    except TimeoutError:
        proc.kill()
        logger.warning("list_agents.sh timed out after 5 seconds")
        return []

    if proc.returncode is None:  # pragma: no cover — always set after communicate()
        return []
    if proc.returncode != 0:
        stderr_snippet = stderr_bytes[:500].decode("utf-8", errors="replace")
        logger.warning(
            "list_agents.sh exited with code %d. stderr: %s",
            proc.returncode,
            stderr_snippet,
        )
        return []

    # Decode stdout line-by-line, dropping lines with invalid UTF-8
    decoded_lines: list[str] = []
    for raw_line in stdout_bytes.split(b"\n"):
        try:
            decoded_lines.append(raw_line.decode("utf-8"))
        except UnicodeDecodeError:
            logger.warning("list_agents.sh emitted a line with invalid UTF-8, dropping")

    return parse_list_agents_output("\n".join(decoded_lines))
